"""控制主循环：读取观测、诊断、选路，再推进命令执行。

阅读顺序：tick → _review_network → decision_policy；路径确定后进入 _begin。
命令实际到达节点才会执行，回执实际到达控制端才调用 receive_ack。
本文件集中保存控制端的执行记录；不持有 Simulation 或远端节点对象。
当前仍逐业务、逐命令串行处理，旧路径先停止并清理，再部署新路径。
"""
from collections import defaultdict
from copy import deepcopy

from .diagnosis import Diagnosis, diagnose, is_active_demand, validate_diagnosis
from .interfaces import DecisionPolicy, validate_policy_result
from .policy import decide


class ControlLoop:
    def __init__(self, cache, service_cache, send, log, config, *,
                 decision_policy: DecisionPolicy | None = None, diagnosis_policy = None):
        for name, function in (('decision_policy', decision_policy),
                               ('diagnosis_policy', diagnosis_policy)):
            if function is not None and not callable(function):
                raise TypeError(f'{name} 必须是可调用的函数或 None')

        self.link_reports = cache                  # 已到达的链路报告。
        self.service_reports = service_cache  # 已到达的业务需求报告。
        self.send, self.log, self.config = send, log, config
        self.diagnosis_policy = diagnose if diagnosis_policy is None else diagnosis_policy
        self.decision_policy = decide if decision_policy is None else decision_policy
        self.diagnosis = Diagnosis((), ())  # 最近一次诊断结果，供阅读与调试。

        # 以下都是“控制端已知的执行进度”，不代表远端瞬时真实状态。
        self.versions = {}    # 节点版本：只由收到的报告或回执更新。
        self.confirmed = {}   # 已收到成功回执的业务方案。
        self.retained = []    # 等待确认清理、仍需占预算的旧方案。
        self.transaction = None  # 当前业务的部署、停源或清理流程。
        self.pending = None      # 当前等待回执的那一条命令。
        self.epoch = defaultdict(int)
        self.command_count = self.plan_count = 0
        self.last_decision_ns = - 10**18
        self.cursor = None

    def tick(self, now):
        """由仿真时钟调用：先处理未完成命令，再开始下一轮诊断与决策。"""
        if self.pending or self.transaction:
            self._advance_pending(now)
            return
        if now - self.last_decision_ns < self.config['decision_interval_ns']:
            return
        self.last_decision_ns = now
        self._review_network(now)

    def _review_network(self, now):
        """获取观测 → 运维诊断 → 资源决策 → 开始下发；各阶段输入在本方法可见。"""
        snapshot = self.link_reports.snapshot(now)
        demand = self.service_reports.snapshot(now)
        self.log({'event': 'snapshot', 'time_ns': now, 'snapshot': snapshot})
        self.log({'event': 'service_snapshot', 'time_ns': now, 'snapshot': demand})
        # 运维替换入口：只传递已到达的观测副本，结果不能创建未收到的需求。
        diagnosis = self.diagnosis_policy(deepcopy(snapshot), deepcopy(demand), now_ns = now)
        validate_diagnosis(diagnosis, demand)
        self.diagnosis = diagnosis
        allowed = set(diagnosis.active_service_ids)
        for node_id, version in snapshot['node_versions'].items():
            self.versions[node_id] = max(self.versions.get(node_id, 0), version)
        # 已停止或失去新鲜报告的已确认业务优先停源，释放资源前仍须带内确认。
        for service_id in sorted(self.confirmed):
            row = demand['services'].get(service_id)
            if service_id not in allowed:
                reason = ('inactive_or_stale_demand' if not is_active_demand(row)
                          else 'diagnosis_filtered_demand')
                self._stop(now, service_id, reason)
                return
        candidates = sorted(
            allowed,
            key = lambda service_id: ( - demand['services'][service_id]['service']['priority'], service_id),
        )
        if self.cursor in candidates:
            start = (candidates.index(self.cursor) + 1) % len(candidates)
            candidates = candidates[start: ] + candidates[: start]
        for service_id in candidates:
            row = demand['services'][service_id]
            old = self.confirmed.get(service_id)
            retained = self.budgets()
            # 检查已有路径时不把自身占用扣两次；提交新路径前先明确退役旧路径。
            if old:
                for link_id in old['decision']['link_path']:
                    retained[link_id] -= old['decision']['wire_bps']
            # 替换决策：通过构造器注入同签名函数；输入只有已到达的观测与需求。
            # 返回路径建议后仍须经过本类事务、带内传输和节点执行校验。
            # 当前每次评估一项业务；多业务联合优化需要同步调整此候选循环。
            service = self.service_reports.get_spec(service_id)
            result = self.decision_policy(
                deepcopy(snapshot), [service], now_ns = now,
                payload_bytes = 1000, epoch_overhead_bytes = 8,
                control_headroom_fraction = self.config['control_headroom_fraction'],
                retained_wire_bps = dict(retained),
            )
            validate_policy_result(result, service, snapshot, now_ns = now)
            self.log({'event': 'decision', 'time_ns': now, 'plan_id': None, 'decision': result,
                      'service_id': service_id, 'checked_revision': row['revision'],
                      'self_budget_excluded': bool(old)})
            choice = result['decisions'][0]
            if old:
                unchanged = (
                    choice['accepted']
                    and old['decision']['link_path'] == choice['link_path']
                    and old['decision']['reserved_bps'] == choice['reserved_bps']
                    and old['decision']['demand_revision'] == row['revision']
                )
                if unchanged:
                    continue
                self.cursor = service_id
                reason = 'path_or_demand_change' if choice['accepted'] else choice['reason']
                self._stop(now, service_id, reason)
                return
            if choice['accepted']:
                self.cursor = service_id
                choice = deepcopy(choice) | {'demand_revision': row['revision']}
                self._begin(now, choice)
                return

    def _eligible(self, service_id, revision, now):
        """收到每一步回执后重新核对需求；等待期间报告可能过期或需求已变化。"""
        row = self.service_reports.snapshot(now)['services'].get(service_id)
        return is_active_demand(row) and row['revision'] == revision

    def budgets(self):
        """已确认和等待清理的旧路径都占预算；不把尚未收到清理回执当成释放。"""
        result = defaultdict(int)
        items = list(self.confirmed.values()) + self.retained
        # 正在提交或执行结果未知的新路径也占账面预算；不能在等待query时消失。
        if self.transaction and not self.transaction.get('source_confirmed'):
            items.append({'epoch': self.transaction['epoch'], 'decision': self.transaction['decision']})
        seen = set()
        for item in items:
            identity = (item['decision']['service_id'], item['epoch'])
            if identity in seen:
                continue
            seen.add(identity)
            for link_id in item['decision']['link_path']:
                result[link_id] += item['decision']['wire_bps']
        return dict(result)

    def _advance_pending(self, now):
        """处理等待、重试和查询；这些步骤不运行选路算法。"""
        if self.pending:
            pending = self.pending
            transaction = self.transaction
            if (now >= transaction['deadline_ns'] and not transaction['deadline_exceeded']
                    and pending['command']['action']['op'] in ('prepare', 'install', 'activate')):
                # 已发出的activate可能已经生效，期限耗尽时必须先查询，不能盲目回滚。
                transaction['deadline_exceeded'] = True
                self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'],
                          'phase': 'deadline', 'status': 'unknown'})
                uncertain = pending['command']
                self.pending = None
                self._query(now, uncertain)
                return
            if now - pending['sent_at_ns'] < self.config['ack_wait_ns']:
                return
            command = pending['command']
            if pending['retries'] < self.config['max_retries'] and now - command['issued_at_ns'] <= command['validity_ns']:
                pending['retries'] += 1
                pending['sent_at_ns'] = now
                self.send(command, retry = True)  # command_id、签发时刻和完整内容均保持原样。
                return
            self.log({'event': 'command_timeout', 'time_ns': now, 'command_id': command['command_id'],
                      'plan_id': command['plan_id'], 'state': 'unknown', 'operation': command['action']['op']})
            self.pending = None
            if command['action']['op'] == 'query':
                self.transaction['status'] = 'unknown'  # 未查明前保留全部新旧规则和预算。
                self.transaction['resume_at_ns'] = now + self.config['query_interval_ns']
            else:
                self._query(now, command)
            return
        if self.transaction:
            transaction = self.transaction
            if transaction['status'] == 'unknown' and now >= transaction['resume_at_ns']:
                self._query(now, transaction['uncertain_command'])
            elif transaction['status'] == 'retaining' and now >= transaction['resume_at_ns']:
                self._cleanup_old(now)
            return

    def _begin(self, now, decision):
        if self.transaction is not None or self.pending is not None:
            raise RuntimeError('已有方案正在执行或查询，不能覆盖未完成事务')
        service_id = decision['service_id']
        self.epoch[service_id] += 1
        self.plan_count += 1
        epoch = self.epoch[service_id]
        plan = f'P{self.plan_count:04d}'
        lease = now + self.config['prepare_lease_ns']
        steps = []
        # 目标接收节点不用安装下一跳；其余节点从离目标最近的位置开始处理。
        hops = list(zip(decision['node_path'][:-1], decision['node_path'][1:], decision['link_path']))
        for op in ('prepare', 'install'):
            for node, peer, link_id in reversed(hops):
                steps.append((node, {'op': op, 'service_id': service_id, 'epoch': epoch, 'link_id': link_id,
                                     'next_node_id': peer, 'reserved_bps': decision['reserved_bps'], 'lease_until_ns': lease}))
        steps.append((decision['node_path'][0], {'op': 'activate', 'service_id': service_id, 'epoch': epoch,
                                               'reserved_bps': decision['reserved_bps']}))
        self.transaction = {'plan_id': plan, 'decision': deepcopy(decision), 'epoch': epoch, 'steps': steps,
                            'index': 0, 'status': 'deploying', 'touched': [], 'old': self.confirmed.get(service_id),
                            'source_confirmed': False, 'deadline_exceeded': False,
                            'deadline_ns': now + self.config.get('transaction_timeout_ns', self.config['prepare_lease_ns'])}
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': plan, 'phase': 'begin', 'status': 'pending',
                  'decision': decision, 'epoch': epoch})
        self._next(now)

    def _stop(self, now, service_id, reason):
        old = self.confirmed[service_id]
        self.plan_count += 1
        plan = f'P{self.plan_count:04d}'
        self.transaction = {'plan_id': plan, 'decision': deepcopy(old['decision']), 'epoch': old['epoch'],
            'old': old, 'steps': [], 'index': 0, 'status': 'stopping', 'touched': [],
            'source_confirmed': False, 'deadline_exceeded': False,
            'deadline_ns': now + self.config.get('transaction_timeout_ns', self.config['prepare_lease_ns'])}
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': plan, 'phase': 'stop_begin', 'status': 'pending',
                  'reason': reason, 'decision': old['decision'], 'epoch': old['epoch']})
        self._issue(now, old['decision']['node_path'][0], {'op': 'deactivate', 'service_id': service_id, 'epoch': old['epoch']})

    def _issue(self, now, node, action):
        self.command_count += 1
        base = self.versions.get(node, 0)
        command = {'kind': 'command', 'command_id': f'C{self.command_count:05d}',
                   'plan_id': self.transaction['plan_id'], 'target_node_id': node,
                   'issued_at_ns': now, 'validity_ns': self.config['command_validity_ns'],
                   'base_version': base, 'new_version': base + (action['op'] != 'query'), 'action': action}
        self.pending = {'command': command, 'sent_at_ns': now, 'retries': 0}
        self.send(command, retry = False)

    def _next(self, now):
        """每个准备/安装回执到达后重新检查需求；过期需求不能继续安装并启用。"""
        transaction = self.transaction
        if transaction['status'] == 'deploying':
            decision = transaction['decision']
            if not self._eligible(decision['service_id'], decision['demand_revision'], now):
                self._abort(now, 'demand_changed_during_deployment')
                return
            # 启用命令带上需求版本；节点据此拦截已停止或重启前的旧需求。
            if transaction['index'] < len(transaction['steps']):
                node, action = transaction['steps'][transaction['index']]
                if action['op'] == 'activate':
                    action['demand_revision'] = decision['demand_revision']

        if transaction['status'] == 'deploying' and now >= transaction['deadline_ns']:
            transaction['deadline_exceeded'] = True
            self._abort(now, 'transaction_deadline')
            return
        if transaction['index'] < len(transaction['steps']):
            node, action = transaction['steps'][transaction['index']]
            self._issue(now, node, action)
            return
        if transaction['status'] == 'aborting':
            self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'abort_complete', 'status': 'aborted'})
            self.transaction = None
        elif transaction['status'] == 'cleaning':
            if transaction['old'] in self.retained:
                self.retained.remove(transaction['old'])
            self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'cleanup_complete', 'status': 'confirmed'})
            self.transaction = None

    def receive_ack(self, ack, now, source_node):
        """网络送达回执后，核对身份与版本，再更新控制端记录并推进下一步。"""
        pending = self.pending
        if not pending:
            return
        command = pending['command']
        transaction = self.transaction
        if (ack.get('command_id') != command['command_id'] or ack.get('plan_id') != command['plan_id']
                or ack.get('node_id') != source_node or source_node != command['target_node_id']
                or type(ack.get('version')) is not int or ack['version'] < 0):
            return
        status = ack.get('original_status', ack.get('status')) if ack.get('status') == 'duplicate' else ack.get('status')
        op = command['action']['op']
        if op == 'deactivate':
            self.versions[source_node] = max(self.versions.get(source_node, 0), ack['version'])
            self.pending = None
            if (status == 'applied' and ack['version'] == command['new_version']
                    and ack.get('source_enabled') is False and ack.get('active_epoch') == transaction['epoch']):
                self._deactivated(now)
            else:
                self._query(now, command)
            return
        if op == 'query' and transaction['uncertain_command']['action']['op'] == 'deactivate':
            self.versions[source_node] = max(self.versions.get(source_node, 0), ack['version'])
            self.pending = None
            if (status == 'applied' and ack.get('source_enabled') is False and ack.get('active_epoch') == transaction['epoch']):
                self._deactivated(now, reconciled = True)
            elif status == 'applied' and ack.get('source_enabled') is True:
                # 源端仍开着就重新下发停源命令；重复停源不会重新启用任何版本。
                self._issue(now, source_node, deepcopy(transaction['uncertain_command']['action']))
            else:
                transaction.update(status = 'unknown', resume_at_ns = now + self.config['query_interval_ns'])
            return
        if op == 'query' and transaction['uncertain_command']['action']['op'] == 'activate' and ack.get('source_enabled') is not True:
            self.versions[source_node] = max(self.versions.get(source_node, 0), ack['version'])
            self.pending = None
            uncertain = transaction['uncertain_command']
            query_time = ack.get('original_executed_at_ns') if ack.get('status') == 'duplicate' else ack.get('executed_at_ns')
            if type(query_time) is not int or not 0 <= query_time <= now:
                query_time = command['issued_at_ns']
            if status == 'applied' and query_time > uncertain['issued_at_ns'] + uncertain['validity_ns']:
                self._abort(now, 'activation_not_enabled_after_expiry')
            else:
                transaction.update(status = 'unknown', resume_at_ns = max(now + self.config['query_interval_ns'],
                    uncertain['issued_at_ns'] + uncertain['validity_ns'] + 1))
            return

        self.versions[source_node] = max(self.versions.get(source_node, 0), ack['version'])
        self.pending = None
        transaction = self.transaction
        if command['action']['op'] == 'query':
            uncertain = transaction['uncertain_command']
            query_time = (ack.get('original_executed_at_ns') if ack.get('status') == 'duplicate'
                          else ack.get('executed_at_ns'))
            if type(query_time) is not int or not 0 <= query_time <= now:
                # 签发时刻是查询实际执行时刻的保守下界，不用未校验的远端时间放行。
                query_time = command['issued_at_ns']
            if status != 'applied':
                transaction.update(status = 'unknown', resume_at_ns = now + self.config['query_interval_ns'])
            elif uncertain['action']['op'] == 'activate' and ack.get('active_epoch') == transaction['epoch']:
                self._activated(now, reconciled = True)
            elif (uncertain['action']['op'] == 'activate'
                    and query_time <= uncertain['issued_at_ns'] + uncertain['validity_ns']):
                # 查询先到、activate仍在路上时，“尚未启用”不能证明最终未执行。
                # 等原指令失效后再次查询，期间保留整条新路径，避免先清中间节点。
                transaction.update(status = 'unknown', resume_at_ns = max(now + self.config['query_interval_ns'],
                    uncertain['issued_at_ns'] + uncertain['validity_ns'] + 1))
            elif uncertain['action']['op'] in ('cleanup', 'abort'):
                if (uncertain['action']['op'] == 'abort'
                        and uncertain['target_node_id'] == transaction['decision']['node_path'][0]
                        and ack.get('active_epoch') == transaction['epoch']):
                    # 查询证实源已切到新epoch；保留新规则并按正常旧规则清理完成。
                    self._activated(now, reconciled = True)
                else:
                    # 查询只恢复版本认知，不证明规则已删除。发一个新ID的幂等清理，
                    # 保留原index，收到明确清理ACK后才进入下一节点并释放预算。
                    transaction['status'] = 'cleaning' if uncertain['action']['op'] == 'cleanup' else 'aborting'
                    self._issue(now, uncertain['target_node_id'], deepcopy(uncertain['action']))
            else:
                self._abort(now, 'unconfirmed_step')
            return
        if status != 'applied' or ack['version'] != command['new_version']:
            if command['action']['op'] in ('cleanup', 'abort'):
                self._query(now, command)
            elif command['action']['op'] == 'activate' and status == 'applied':
                # 不完整的成功ACK不能当作明确失败去删除可能已启用的规则。
                self._query(now, command)
            else:
                self._abort(now, ack.get('reason', 'invalid_ack'))
            return
        if command['action']['op'] == 'activate':
            self._activated(now)
            return
        if command['action']['op'] in ('prepare', 'install') and source_node not in transaction['touched']:
            transaction['touched'].append(source_node)
        transaction['index'] += 1
        self._next(now)

    def _activated(self, now, reconciled = False):
        transaction = self.transaction
        service_id = transaction['decision']['service_id']
        transaction['source_confirmed'] = True
        if transaction['old'] and transaction['old'] not in self.retained:
            self.retained.append(transaction['old'])
        self.confirmed[service_id] = {'epoch': transaction['epoch'], 'decision': transaction['decision'], 'confirmed_at_ns': now}
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'source_confirmed',
                  'status': 'confirmed', 'epoch': transaction['epoch'], 'reconciled_by_query': reconciled})
        transaction.update(status = 'retaining', resume_at_ns = now + self.config['old_rule_retention_ns'])
        if not transaction['old']:
            self.transaction = None

    def _deactivated(self, now, reconciled = False):
        transaction = self.transaction
        service_id = transaction['decision']['service_id']
        old = transaction['old']
        if old not in self.retained:
            self.retained.append(old)
        self.confirmed.pop(service_id, None)
        transaction.update(source_confirmed = True, status = 'retaining', resume_at_ns = now + self.config['old_rule_retention_ns'])
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'source_stopped',
                  'status': 'confirmed', 'service_id': service_id, 'epoch': transaction['epoch'], 'reconciled_by_query': reconciled})

    def _cleanup_old(self, now):
        transaction = self.transaction
        old = transaction['old']
        service_id = transaction['decision']['service_id']
        # 最终目的虽无下一跳规则，也必须退役该epoch，拦截最后一跳仍在传播的旧包。
        # 整条旧路径每个节点确认后，_next才释放对应预算。
        transaction['steps'] = [(node_id, {'op': 'cleanup', 'service_id': service_id, 'epoch': old['epoch']})
                      for node_id in reversed(old['decision']['node_path'])]
        transaction.update(index = 0, status = 'cleaning')
        self._next(now)

    def _abort(self, now, reason):
        transaction = self.transaction
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'partial_failure',
                  'status': 'partial', 'reason': reason, 'touched_nodes': transaction['touched']})
        if transaction['status'] in ('cleaning', 'aborting'):
            # 清理失败保留预算；不得把“发出了清理请求”当成已释放资源。
            transaction.update(status = 'unknown', resume_at_ns = now + self.config['query_interval_ns'])
            # 调用方应保存具体不确定命令；绝不伪造指向新active epoch的query动作。
            if 'uncertain_command' not in transaction:
                raise RuntimeError('清理失败缺少待查询的具体命令')
            return
        transaction['steps'] = [(node_id, {'op': 'abort', 'service_id': transaction['decision']['service_id'], 'epoch': transaction['epoch']})
                      for node_id in reversed(transaction['decision']['node_path'])]
        transaction.update(index = 0, status = 'aborting')
        self._next(now)

    def _query(self, now, uncertain):
        transaction = self.transaction
        transaction['uncertain_command'] = deepcopy(uncertain)
        transaction['status'] = 'querying'
        self.log({'event': 'transaction', 'time_ns': now, 'plan_id': transaction['plan_id'], 'phase': 'query', 'status': 'unknown'})
        self._issue(now, uncertain['target_node_id'], {'op': 'query', 'service_id': transaction['decision']['service_id'], 'epoch': transaction['epoch']})

