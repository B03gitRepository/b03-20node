"""网络控制入口：连接报告、控制主流程与节点执行器。

读取顺序：setup() 安排周期任务 → receive() 按到达消息分发 →
_control_tick() 调用 loop.tick() 完成诊断、资源决策及命令推进。
策略在 diagnosis.py / policy.py；执行步骤在 loop.py / executor.py。

本文件不维护业务需求副本。业务定义读取 sim.services，本地需求读取
sim.demands；二者只有 Simulation 负责维护。控制端收到的远端报告独立
保存在 link_reports / service_reports，消息送达后才更新。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from time import perf_counter_ns
from functools import partial

from .observation import ObservationCache, ServiceObservationCache, describe_service
from .loop import ControlLoop
from .config import control_defaults
from .interfaces import DecisionPolicy
from .executor import NodeExecutor
from .policy import decide


class NetworkController:
    """一轮仿真的带内控制装配器；setup 只调用一次，summary 不改变运行状态。"""

    def __init__(self, sim, options=None, *, decision_policy: DecisionPolicy | None = None, diagnosis_policy=None):
        self.sim = sim
        self.node_ids = sorted(n.node_id for n in sim.scenario.nodes)
        self.options = control_defaults(options, service_ids=self.sim.services, node_ids=self.node_ids)
        self.controller_id = self.options['controller_id']
        self.start_ns = round(sim.config.get('start_s', 0) * 1e9)
        self.end_ns = round(sim.config.get('end_s', sim.scenario.duration_s) * 1e9)
        self.stop_ns = self.end_ns + round(sim.config.get('drain_s', 0) * 1e9)
        self.managed = set(self.options['managed_service_ids'])
        self.endpoints = {l.link_id: (l.src_node_id, l.dst_node_id) for l in sim.scenario.links}
        self.link_reports = ObservationCache(self.endpoints, self.options['observation_ttl_ns'])
        self.service_reports = ServiceObservationCache(self.node_ids, self.options['observation_ttl_ns'])
        # 官方场景显式采用官方配置级 QoS 合成及拒绝规则。
        # 参数绑定只作用于内置策略；外部智能体仍使用原有 DecisionPolicy 接口。
        if decision_policy is None and self.options['qos_profile'] == 'official':
            decision_policy = partial(decide, qos_profile='official')
        # 这里只把收到报告后的两个缓存给协调器。未来业务目录和实时全网状态不传入。
        self.loop = ControlLoop(self.link_reports, self.service_reports, self.send_command,
                                              self._controller_log, self.options, decision_policy=decision_policy,
                                              diagnosis_policy=diagnosis_policy)
        self.node_executors = {}
        self.probe_sequences = defaultdict(int)
        self.service_sequences = defaultdict(int)
        self.counts = Counter()
        self.execution_statuses = Counter()
        self.execution_reasons = Counter()
        self._setup_done = False
        self.decision_wall_time_ns = 0
        primary, backup = self.options['control_primary_link'], self.options['control_backup_link']
        if primary:
            if primary == backup or primary not in self.endpoints or backup not in self.endpoints:
                raise ValueError('管控主备应为两条存在的不同链路')
            if set(self.endpoints[primary]) != set(self.endpoints[backup]):
                raise ValueError('管控主备必须连接同一对节点')

    def now(self):
        """读取仿真时刻，既不等待现实时间，也不推进模拟时钟。"""
        return int(self.sim.env.now)

    def _controller_log(self, row):
        if row.get('event') == 'transaction':
            self.counts['transaction_' + row.get('phase', 'unknown')] += 1
        self.sim.log('controller', row)

    def _execution_log(self, row):
        self.execution_statuses[row['status']] += 1
        self.execution_reasons[row['reason']] += 1
        self.sim.log('executions', row)

    def setup(self):
        """创建本地执行器，安排周期报告和规则检查；此时不会越过网络写入缓存。"""
        if self._setup_done:
            raise RuntimeError('同一控制器不能重复 setup')
        self._setup_done = True
        for nid in self.node_ids:
            neighbors = {lid: pair[1] if pair[0] == nid else pair[0]
                         for lid, pair in self.endpoints.items() if nid in pair}
            # partial 绑定当前节点编号；每个执行方法只作用于这个节点。
            self.node_executors[nid] = NodeExecutor(
                node_id=nid, controller_id=self.controller_id,
                services=self.sim.services, neighbors=neighbors,
                install_rule=partial(self.sim.install_rule, nid),
                remove_rule=partial(self.sim.remove_rule, nid),
                activate_source=partial(self.sim.activate_source, nid),
                logger=self._execution_log, now_fn=self.now,
                demand_state=partial(self.sim.local_demand, nid),
                deactivate_source=partial(self.sim.deactivate_source, nid))
        for sid in sorted(self.managed):
            self.sim.sources[sid].set_rate(0)
        self.sim.sync_demands()
        for index, nid in enumerate(self.node_ids):
            # 报告稍微错开发送，采用确定性的启动安排；不减少实际报文数量。
            self._schedule(self.start_ns + 100_000_000 + index * 1_000_000,
                           lambda n=nid: self._probe_tick(n))
            self._schedule(self.start_ns + 150_000_000 + index * 1_000_000,
                           lambda n=nid: self._service_tick(n))
        self._schedule(self.start_ns + 1_000_000_000, self._control_tick)
        if self.options['control_primary_link']:
            self._schedule(self.start_ns + 10_000_000, self._failover_tick)

    def _schedule(self, at_ns, callback):
        """不安排终点之后的重复定时器，避免暂停环境时留下无意义的周期尾巴。"""
        if self.now() <= at_ns <= self.stop_ns:
            self.sim.schedule(at_ns, callback)

    def on_demand_changed(self, sid):
        """本地需求已更新：撤销旧授权，并安排新报告经过网络发送。

        由 Simulation.sync_demands 调用。sim.demands 是事实来源；这里仅
        同步执行器的授权记录。控制端的远端观测仍须等报告真正到达。
        """
        state = self.sim.demands[sid]
        nid = self.sim.services[sid].src_node_id
        executor = self.node_executors[nid]
        was_enabled = executor.source_enabled.get(sid, False)
        self.sim.sources[sid].set_rate(0)
        executor.source_enabled[sid] = False
        self.sim.log('service-local', {'event': 'local_demand_changed', 'time_ns': self.now(),
                     'node_id': nid, 'service_id': sid, **state,
                     'source_permission_revoked': True, 'previous_source_enabled': was_enabled})
        if state['registered'] and self.start_ns <= self.now() < self.stop_ns:
            self._schedule(self.now() + 1, partial(self.send_service_status, sid))

    def _send(self, src, dst, message):
        # Simulation.send_control 统一完成编码校验和网络入队，按消息字节数计费。
        sent_bytes = self.sim.send_control(src, dst, message)
        self.counts['send_attempt_' + message['kind']] += 1
        return sent_bytes

    def send_command(self, command, retry=False):
        """发送原签发时间的命令；重试不会重置五秒有效期。"""
        self.sim.log('controller', {'event': 'command_sent', 'time_ns': self.now(),
                                   'command': deepcopy(command), 'retry': retry})
        return self._send(self.controller_id, command['target_node_id'], command)

    def send_probes(self, nid):
        """各节点只采样自己的关联端口，报告到达前不更新控制器缓存。"""
        if not self.sim.state.node_online[nid]:
            return
        for lid, pair in sorted(self.endpoints.items()):
            if nid not in pair:
                continue
            snapshot = self.sim.links[lid].snapshot()
            params = snapshot['parameters']
            self.probe_sequences[nid, lid] += 1
            probe = {'kind': 'probe', 'node_id': nid, 'seq': self.probe_sequences[nid, lid],
                     'observed_at_ns': self.now(), 'node_online': True,
                     'version': self.node_executors[nid].version,
                     'link': {'link_id': lid, 'peer_node_id': pair[1] if pair[0] == nid else pair[0],
                              'admin_up': snapshot['admin_up'], 'endpoint_online': snapshot['effective_up'],
                              'capacity_bps': int(params['capacity_bps']),
                              'delay_ns': round(params['delay_ms'] * 1e6),
                              'jitter_ns': round(params['jitter_ms'] * 1e6),
                              'packet_loss_rate': params['packet_loss_rate'], 'ber': params['ber'],
                              'queue_packets': snapshot['queue_packets']}}
            self._send(nid, self.controller_id, probe)

    def send_service_status(self, sid):
        state = self.sim.demands[sid]
        service = self.sim.services[sid]
        if not state['registered'] or not self.sim.state.node_online[service.src_node_id]:
            return
        self.service_sequences[sid] += 1
        message = {'kind': 'service_status', 'node_id': service.src_node_id,
                   'seq': self.service_sequences[sid], 'observed_at_ns': self.now(),
                   **state, 'service': describe_service(service)}
        self._send(service.src_node_id, self.controller_id, message)

    def receive(self, nid, message, source, uid):
        """只由网络实际投递调用。source 是包的源节点，不能从消息自报字段推定。"""
        kind = message.get('kind')
        self.counts['received_' + str(kind)] += 1
        now = self.now()
        # 报告只在此处入库；运维替换入口是 diagnosis_policy，见 loop.tick。
        # 不在报告发送处更新远端观测，不向诊断函数传入 sim.state 或未来事件。
        if kind == 'probe' and nid == self.controller_id:
            result = self.link_reports.receive(message, now, source)
            self.counts['probe_' + result['reason']] += 1
            self.sim.log('controller', {'event': 'observation', 'time_ns': now,
                                       'packet_uid': uid, 'probe': message, 'result': result})
        elif kind == 'service_status' and nid == self.controller_id:
            result = self.service_reports.receive(message, now, source)
            self.counts['service_' + result['reason']] += 1
            self.sim.log('controller', {'event': 'service_observation', 'time_ns': now,
                                       'packet_uid': uid, 'message': message, 'result': result})
        elif kind == 'command':
            ack = self.node_executors[nid].receive(message, now, uid, source)
            # 本地执行和远端确认是两个事件：回执必须再次进入网络排队。
            self._send(nid, self.controller_id, ack)
        elif kind == 'ack' and nid == self.controller_id:
            self.sim.log('controller', {'event': 'ack_received', 'time_ns': now,
                                       'packet_uid': uid, 'ack': message})
            self.loop.receive_ack(message, now, source)

    def _probe_tick(self, nid):
        self.send_probes(nid)
        self._schedule(self.now() + self.options['probe_interval_ns'], lambda: self._probe_tick(nid))

    def _service_tick(self, nid):
        for sid in sorted(self.managed):
            if self.sim.services[sid].src_node_id == nid:
                self.send_service_status(sid)
        self._schedule(self.now() + self.options['service_report_interval_ns'], lambda: self._service_tick(nid))

    def _control_tick(self):
        # 现实计算耗时单独记录；模拟计算耗时当前设为 0。
        start = perf_counter_ns()
        if self.sim.state.node_online[self.controller_id]:
            self.loop.tick(self.now())
        self.decision_wall_time_ns += perf_counter_ns() - start
        self._schedule(self.now() + self.options['tick_ns'], self._control_tick)

    def _failover_tick(self):
        self.sim.local_failover(self.options['control_primary_link'], self.options['control_backup_link'])
        self._schedule(self.now() + self.options['port_detection_ns'], self._failover_tick)

    def summary(self):
        """输出可序列化的实际末态；未知事务、保留预算和在途命令不强行清零。"""
        return deepcopy({
            'counts': dict(self.counts),
            'execution_statuses': dict(self.execution_statuses),
            'execution_reasons': dict(self.execution_reasons),
            'confirmed': self.loop.confirmed, 'retained': self.loop.retained,
            'pending': self.loop.pending, 'transaction': self.loop.transaction,
            'retained_wire_bps': self.loop.budgets(),
            'node_versions': {nid: e.version for nid, e in self.node_executors.items()},
            'local_active_epochs': {nid: e.active_epochs for nid, e in self.node_executors.items()},
            'local_source_enabled': {nid: e.source_enabled for nid, e in self.node_executors.items()},
            'local_demands': self.sim.demands,
            'service_snapshot': self.service_reports.snapshot(self.now()),
            'observation_ttl_ns': self.options['observation_ttl_ns'],
            'command_validity_ns': self.options['command_validity_ns'],
            'control_end_ns': self.stop_ns,
            'decision_compute_mode': 'zero_simulated_compute_delay_python_deterministic_baseline',
            'path_change_mode': 'stop_confirm_cleanup_then_redeploy',
        })
