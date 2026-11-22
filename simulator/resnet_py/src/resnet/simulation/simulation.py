"""连接网络内核、业务、场景事件与控制器的一轮仿真。

Simulation 组装网络并统一执行场景事件。业务源产生 Packet，ResourceLink
负责每一跳的排队、发送与传播；到达节点后交回 handle_packet_at_node 处理。
输入 ScenarioSpec 和运行配置；依赖 SimPy、link、traffic、collector；输出
summary() 与 JSONL 证据。不同 Simulation 实例没有共享的仿真全局状态。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import simpy

from resnet.metrics.collector import Collector
from .engine import Call, advance_inclusive
from .link import ResourceLink
from .packet import Packet
from .state import SceneState, service_is_active
from .topology import initial_routes, stable_link_seed
from .traffic import TrafficSource, RATE_FACTORS
from resnet.contracts.control import encode_message, decode_message, MAX_UDP_PAYLOAD_BYTES


DEFAULT_CONFIG = dict(seed=73, queue_packets=128, queue_bytes=131072,
    payload_bytes=1000, rate_scale=1.0, start_s=0.0, end_s=None, drain_s=240.0,
    mode='fixed', service_ids=None, sample_modulus=10000, control={},
    log_control=True, progress_interval_s=60)

# 事件字段到链路参数的对应关系；保留官方数据中的兼容拼写。
# 事件和 ResourceLink 均使用毫秒时延、bit/s 容量，此处只转换字段名。
PARAMETER_MAP = {
    'bandwidthBps': 'capacity_bps', 'bandwitdhBps': 'capacity_bps',
    'delayMs': 'delay_ms', 'jitterMs': 'jitter_ms',
    'ber': 'ber', 'packetLossRate': 'packet_loss_rate',
}


class Simulation:
    """阅读主线：初始化 → _apply_batch/_apply_event → handle_packet_at_node。

    state 保存节点与业务安排；links 保存链路当前参数和传输状态；
    sources 保存实际发包状态；routes/rules 保存实际转发规则。
    advance 推进时钟，按已登记的时间调用上述功能。
    """
    def __init__(self, scenario, config=None, output_dir=None, *, decision_policy=None, diagnosis_policy=None):
        self.scenario = scenario
        if config is not None and not isinstance(config, dict):
            raise ValueError('运行配置必须是字典')
        unknown = set(config or {}) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f'未知运行配置项: {sorted(unknown)}')
        self.config = deepcopy(DEFAULT_CONFIG | (config or {}))
        if self.config['end_s'] is None:
            self.config['end_s'] = scenario.duration_s
        self._validate()
        if (decision_policy is not None or diagnosis_policy is not None) and self.config['mode'] != 'control':
            raise ValueError('decision_policy 和 diagnosis_policy 仅用于 control 模式')
        self.start_ns = round(self.config['start_s'] * 1e9)
        self.end_ns = round(self.config['end_s'] * 1e9)
        self.stop_ns = self.end_ns + round(self.config['drain_s'] * 1e9)
        self.env = simpy.Environment()
        self.state = SceneState.from_scenario(scenario)
        # 静态业务定义目录，运行时不修改；动态需求与发送状态写入 demands/sources。
        self.services = {s.service_id: s for s in scenario.services + scenario.added_services}
        self.metrics = Collector(self.services)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self._files = {}
        self._closed = False
        self._uid = 0
        self.pending = {}  # 仅保留尚未终结的包；已送达/丢弃立即移出，内存不随总包数增长。
        self.routes = initial_routes(scenario)
        self.initial_routes = dict(self.routes)
        self.rules = {}  # (node_id, service_id, epoch) -> (下一跳,链路ID)
        self.retired = set()
        self.event_count = Counter()
        self.batch_count = 0
        self.local_multipliers = {n.node_id: 0.0 for n in scenario.nodes}
        self._last_demand_ns = 0
        self._demand_bit_ns = {sid: 0 for sid in self.services}
        self.control = None
        self.managed_service_ids = (set(self.config['control'].get('managed_service_ids', self.services))
                                    if self.config['mode'] == 'control' else set())
        # 本地有效需求的唯一记录。场景的 registered/active 是原始活动安排；
        # demands 还叠加实验窗口和业务筛选。只有 sync_demands 写这份字典。
        self.demands = {sid: {'registered': False, 'active': False, 'revision': 0}
                        for sid in sorted(self.managed_service_ids)}
        self.links = {}
        for spec in sorted(scenario.links, key=lambda item: item.link_id):
            # 初始定义只在这里转换一次；以后由对应链路对象保存和更新当前参数。
            parameters = dict(capacity_bps=spec.capacity_bps, delay_ms=spec.delay_s * 1000,
                              jitter_ms=spec.jitter_s * 1000, ber=spec.ber,
                              packet_loss_rate=spec.packet_loss_rate)
            self.links[spec.link_id] = ResourceLink(self.env, spec.link_id,
                (spec.src_node_id, spec.dst_node_id), parameters,
                self.config['queue_packets'], self.config['queue_bytes'],
                stable_link_seed(self.config['seed'], spec.link_id),
                self._link_arrival, self._drop)
        self.sources = {sid: TrafficSource(self, s,
                        0 if self.config['mode'] == 'marl' or sid in self.managed_service_ids else round(s.required_bps * self.config['rate_scale']))
                        for sid, s in self.services.items()}
        # 场景批次最先登记，优先级0；同刻先执行环境变化，再处理网络事件。
        for batch in scenario.batches:
            if batch.time_us * 1000 <= self.end_ns:
                self.schedule(batch.time_us * 1000, lambda b=batch: self._apply_batch(b), priority=0)
        self.schedule(self.start_ns, self.sync_sources, priority=0)
        self.schedule(self.end_ns, self.sync_sources, priority=0)
        if self.config['mode'] == 'control':
            from resnet.control.controller import NetworkController
            self.control = NetworkController(self, self.config['control'],
                decision_policy=decision_policy, diagnosis_policy=diagnosis_policy)
            self.control.setup()

    @property
    def now(self):
        return int(self.env.now)

    def _validate(self):
        import math
        c = self.config
        if c['mode'] not in ('fixed', 'control', 'marl'):
            raise ValueError('mode 必须是 fixed/control/marl')
        for key in ('start_s', 'end_s', 'drain_s', 'rate_scale'):
            if type(c[key]) not in (int, float) or not math.isfinite(c[key]) or c[key] < 0:
                raise ValueError(f'{key} 必须是有限非负数')
        if not 0 <= c['start_s'] < c['end_s'] <= self.scenario.duration_s:
            raise ValueError('窗口必须满足 0 <= start_s < end_s <= 场景时长')
        if not all(math.isfinite(c[k] * 1e9) for k in ('start_s','end_s','drain_s')):
            raise ValueError('时间超出整数纳秒转换范围')
        if round(c['start_s']*1e9) >= round(c['end_s']*1e9):
            raise ValueError('窗口在纳秒精度下必须有正长度')
        if (type(c['progress_interval_s']) not in (int, float)
                or not math.isfinite(c['progress_interval_s']) or c['progress_interval_s'] < 1
                or not math.isfinite(c['progress_interval_s']*1e9)):
            raise ValueError('progress_interval_s 必须是至少1秒的有限数')
        if type(c['log_control']) is not bool:
            raise ValueError('log_control 必须是 bool')
        if not isinstance(c['control'], dict):
            raise ValueError('control 必须是参数字典')
        for key in ('seed', 'queue_packets', 'queue_bytes', 'payload_bytes', 'sample_modulus'):
            if type(c[key]) is not int or c[key] < (0 if key in ('seed','sample_modulus') else 1):
                raise ValueError(f'{key} 需要合法整数')
        if not 12 <= c['payload_bytes'] <= MAX_UDP_PAYLOAD_BYTES:
            raise ValueError('payload_bytes 必须为12至1472字节；当前不模拟分片')
        if c['mode'] == 'control' and (c['payload_bytes'] != 1000 or c['rate_scale'] != 1):
            raise ValueError('普通规则管控要求1000 B与原始需求速率；fixed/marl可单独调参')
        all_ids = {s.service_id for s in self.scenario.services + self.scenario.added_services}
        if c['service_ids'] is not None and (not isinstance(c['service_ids'], list)
                or any(not isinstance(s, str) for s in c['service_ids'])
                or len(set(c['service_ids'])) != len(c['service_ids'])
                or not set(c['service_ids']) <= all_ids):
            raise ValueError('service_ids 需为已登记定义的业务ID列表或null')
        max_rate = c['payload_bytes'] * 8 * 1_000_000_000
        for service in self.scenario.services + self.scenario.added_services:
            rate = service.required_bps * c['rate_scale']
            if not math.isfinite(rate) or rate > max_rate:
                raise ValueError(f'{service.service_id} 的速率超出整数纳秒时钟可表示范围')

    def schedule(self, at_ns, callback, priority=1):
        return Call(self.env, at_ns, callback, priority)

    def log(self, name, row):
        """低频完整记录；没有输出目录时只计算，便于训练不写海量文件。"""
        if self.output_dir is None:
            return
        if name not in self._files:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._files[name] = (self.output_dir / f'{name}.jsonl').open('w', encoding='utf-8')
        self._files[name].write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')

    def _sample(self, event, packet, **extra):
        modulus = self.config['sample_modulus']
        if (packet.kind == 'control' and self.config['log_control']) or (modulus and packet.uid % modulus == 0):
            row = dict(event=event, time_ns=self.now, packet_uid=packet.uid,
                service_id=packet.service_id, kind=packet.kind, epoch=packet.epoch,
                source_node_id=packet.src_node_id, destination_node_id=packet.dst_node_id,
                payload_bytes=packet.payload_bytes, wire_bytes=packet.wire_bytes,
                created_at_ns=packet.created_at_ns, **extra)
            if packet.kind == 'control':
                row['message'] = packet.message
            self.log('packets-control' if packet.kind == 'control' else 'packets-sample', row)

    def _new_packet(self, src, dst, payload_bytes, kind, service_id='', epoch=0, message=None):
        self._uid += 1
        wire = payload_bytes + 28 + (8 if kind == 'data' and service_id in self.managed_service_ids else 0)
        packet = Packet(self._uid, src, dst, payload_bytes, wire, self.now,
                        kind=kind, service_id=service_id, epoch=epoch, message=message)
        self.pending[packet.uid] = packet
        self.metrics.generated(packet, self.now)
        self._sample('generated', packet)
        return packet

    def emit_data(self, sid, epoch=0):
        spec = self.services[sid]
        p = self._new_packet(spec.src_node_id, spec.dst_node_id, self.config['payload_bytes'],
                             'data', sid, epoch)
        self.handle_packet_at_node(p, spec.src_node_id)

    def send_control(self, src, dst, message):
        """JSON按实际UTF8字节计入带宽。远端收件只能由逐跳链路到达触发。"""
        if src not in self.state.node_online or dst not in self.state.node_online:
            raise ValueError('控制消息引用未知端点')
        if (not isinstance(message, dict) or not isinstance(message.get('kind'), str)
                or not message['kind'].strip()):
            raise ValueError('控制消息必须是包含非空字符串kind的字典')
        payload = encode_message(message)
        # 编码再解码，发送方后续修改原字典不会改变已在网络中的消息内容。
        packet = self._new_packet(src, dst, len(payload.encode('utf-8') if isinstance(payload, str) else payload),
                                  'control', message=decode_message(payload))
        if src == dst:
            # 同机调用没有链路传输开销；仍采用一次0延时事件保持非递归收件。
            self.schedule(self.now, lambda p=packet: self.handle_packet_at_node(p, dst))
        else:
            self.handle_packet_at_node(packet, src)
        return packet.payload_bytes

    def handle_packet_at_node(self, packet, nid):
        """处理一个包到达当前节点：终结、交付，或送入下一跳链路。

        读取：节点在线状态，以及 routes/rules 中的转发项。
        修改：包的跳数；交付/丢弃时移出 pending 并更新统计。
        后续：需要转发时调用 ResourceLink.put；本函数不计算链路传输时间。
        """
        if packet.uid not in self.pending:
            raise RuntimeError('已终结包再次进入网络')
        if not self.state.node_online[nid]:
            self._drop(packet, 'node_offline', '')
            return
        versioned = packet.kind == 'data' and packet.service_id in self.managed_service_ids
        key = (nid, packet.service_id, packet.epoch)
        if versioned and key in self.retired:
            self._drop(packet, 'retired_epoch', '')
            return
        if nid == packet.dst_node_id:
            self.pending.pop(packet.uid)
            self.metrics.delivered(packet, self.now)
            self._sample('delivered', packet, delay_ns=self.now - packet.created_at_ns)
            if packet.kind == 'control' and self.control is not None:
                self.control.receive(nid, packet.message, packet.src_node_id, packet.uid)
            return
        if packet.hops >= packet.hop_limit:
            self._drop(packet, 'hop_limit', '')
            return
        route = self.rules.get(key) if versioned else self.routes.get((nid, packet.dst_node_id))
        if route is None:
            self._drop(packet, 'missing_epoch_rule' if versioned else 'no_route', '')
            return
        peer, lid = route
        packet.hops += 1
        self._sample('forward', packet, node_id=nid, next_node_id=peer, link_id=lid)
        self.links[lid].put(packet, nid)

    def _link_arrival(self, packet, peer_node_id, link_id):
        """一跳传播成功后的回调；记录到达，再交给该节点决定后续去向。"""
        self._sample('link_arrival', packet, node_id=peer_node_id, link_id=link_id)
        self.handle_packet_at_node(packet, peer_node_id)

    def _drop(self, packet, reason, link_id=''):
        if self.pending.pop(packet.uid, None) is None:
            raise RuntimeError(f'包 {packet.uid} 重复终结: {reason}')
        self.metrics.dropped(packet, self.now, reason)
        self._sample('dropped', packet, reason=reason, link_id=link_id)

    def _accumulate_demand(self, until_ns):
        """需求积分独立于实际发包速率，关闭所有源不会获得虚假的满分。"""
        duration = max(0, min(until_ns, self.end_ns) - max(self._last_demand_ns, self.start_ns))
        if duration:
            selected = self.config['service_ids']
            for sid in self.state.active & self.state.registered:
                if selected is None or sid in selected:
                    self._demand_bit_ns[sid] += round(self.services[sid].required_bps * self.config['rate_scale']) * duration
        self._last_demand_ns = until_ns

    @property
    def demand_bits_by_service(self):
        return {sid: value / 1e9 for sid, value in self._demand_bit_ns.items()}

    def sync_sources(self):
        """读取场景活动安排，依次刷新各业务源，最后同步受控业务的需求。

        保留活动开关、节点在线和调速的调用顺序；各业务源仍自行安排发包定时器。
        """
        for sid, source in self.sources.items():
            wanted = service_is_active(self.state, sid,
                in_window=self.config['start_s'] <= self.now / 1e9 < self.config['end_s'],
                selected_ids=self.config['service_ids'])
            source.set_active(wanted)
            source.refresh_online()
            if self.config['mode'] == 'marl':
                rate = round(source.service.required_bps * self.config['rate_scale']
                             * self.local_multipliers[source.service.src_node_id]) if wanted else 0
                if source.rate_bps != rate:
                    source.set_rate(rate)
        if self.control is not None:
            self.sync_demands()

    def local_demand(self, node_id, service_id):
        """返回源节点当前需求的副本；节点执行器用它检查过时启用命令。"""
        if self.services[service_id].src_node_id != node_id:
            raise ValueError('需求查询只能用于本节点发起的业务')
        return dict(self.demands.get(service_id,
                    {'registered': False, 'active': False, 'revision': 0}))

    def sync_demands(self):
        """把场景安排转换为本地有效需求；本方法是 demands 的唯一写入入口。

        调用位置：控制初始化，以及 sync_sources 的末尾。需求变化后立即
        撤销旧发送授权，再通知控制入口安排报告。节点离线单独由网络处理，
        不将暂时离线当作业务结束。遍历顺序与原实现一致。
        """
        if self.control is None or not self.control._setup_done:
            raise RuntimeError('先 setup 再同步需求')
        selected = self.config['service_ids']
        for sid in sorted(self.managed_service_ids):
            registered = sid in self.state.registered
            active = service_is_active(self.state, sid,
                in_window=self.start_ns <= self.now < self.end_ns, selected_ids=selected)
            old = self.demands[sid]
            if (old['registered'], old['active']) == (registered, active):
                continue
            self.demands[sid] = {'registered': registered, 'active': active,
                                 'revision': old['revision'] + 1}
            self.control.on_demand_changed(sid)

    def _apply_batch(self, batch):
        """按原顺序执行同刻变化，完成后统一刷新业务源并保存状态快照。"""
        self._accumulate_demand(self.now)
        for operation in batch.operations:
            self._apply_event(operation)
        self.state.profile = batch.profile
        self.batch_count += 1
        self.sync_sources()
        self.log('batches', dict(time_ns=self.now, sequence=batch.sequence, operations=list(batch.operations),
                                state=self.state.snapshot(self.links)))

    def _apply_event(self, operation):
        """执行一条已由加载器校验的操作；各分支直接更新所属对象。

        链路变化调用 ResourceLink 的原有方法，保留中断、队列清理和参数快照规则。
        本方法不刷新业务源；同批次全部操作完成后，由 _apply_batch 统一刷新。
        """
        kind = operation['kind']
        if kind == 'position_sample':
            self.state.positions[operation['node_id']].update(operation['position'])
            return
        if kind == 'traffic_set':
            active = set(operation['active_service_ids'])
            if not active <= self.state.registered:
                raise ValueError('活动业务必须先登记')
            self.state.active = active
            return
        if kind != 'official_event':
            raise ValueError(f'尚未实现的批次操作：{kind}')

        payload = operation['payload']
        event_type, attrs = payload['eventType'], payload['attrs']
        if event_type == 'link_param_change':
            changes = {PARAMETER_MAP[k]: v for k, v in attrs.items() if k in PARAMETER_MAP}
            self.links[attrs['linkId']].update_parameters(**changes)
        elif event_type == 'link_state':
            self.links[attrs['linkId']].set_up(attrs['state'] == 'up')
        elif event_type in ('node_offline', 'node_online'):
            nid, online = attrs['nodeId'], event_type == 'node_online'
            self.state.node_online[nid] = online
            # 按场景原顺序通知关联链路，保持同刻传输事件的登记顺序。
            for spec in self.scenario.links:
                if nid in (spec.src_node_id, spec.dst_node_id):
                    self.links[spec.link_id].set_endpoint_online(nid, online)
        elif event_type == 'service_adjust':
            # 加载器已限定为新增业务；是否开始发送由活动窗决定。
            self.state.registered.add(attrs['serviceId'])
        elif event_type == 'node_move':
            self.state.positions[attrs['nodeId']].update(x=attrs['fromX'], y=attrs['fromY'])
        else:
            raise ValueError(f'尚未实现的事件：{event_type}')
        self.event_count[event_type] += 1

    def position(self, nid):
        """连续线性坐标；链路质量仍由脚本更新，避免位置变化被重复算成衰减。"""
        pos = dict(self.state.positions[nid])
        for move in self.scenario.raw_documents.get('scenario-config.json', {}).get('movements', []):
            if move['node_id'] == nid and move['start_s'] <= self.now / 1e9 <= move['end_s']:
                event = next(e for e in self.scenario.events if e.event_id == move['trigger_event'])
                fraction = (self.now / 1e9 - move['start_s']) / (move['end_s'] - move['start_s'])
                a = event.attrs
                pos.update(x=a['fromX'] + (a['toX']-a['fromX'])*fraction,
                           y=a['fromY'] + (a['toY']-a['fromY'])*fraction)
        return pos

    def install_rule(self, nid, action):
        key = (nid, action['service_id'], action['epoch'])
        if key in self.retired:
            raise ValueError('已退役版本不能重新安装')
        self.rules[key] = (action['next_node_id'], action['link_id'])
        self.log('routes', dict(event='install', time_ns=self.now, node_id=nid, **action))

    def remove_rule(self, nid, action):
        sid, epoch = action['service_id'], action['epoch']
        self.rules.pop((nid, sid, epoch), None)
        self.retired.add((nid, sid, epoch))
        for spec in self.scenario.links:
            if nid in (spec.src_node_id, spec.dst_node_id):
                self.links[spec.link_id].purge(lambda p, source: source == nid and p.service_id == sid and p.epoch == epoch,
                                               'retired_epoch')
        self.log('routes', dict(event='remove', time_ns=self.now, node_id=nid, **action))

    def activate_source(self, nid, action):
        sid = action['service_id']
        if self.services[sid].src_node_id != nid or (nid, sid, action['epoch']) not in self.rules:
            raise ValueError('启用源必须在本机且已有对应版本规则')
        self.sources[sid].epoch = action['epoch']
        self.sources[sid].set_rate(action['reserved_bps'])

    def deactivate_source(self, nid, action):
        sid = action['service_id']
        if self.services[sid].src_node_id != nid:
            raise ValueError('只允许停止本机业务源')
        self.sources[sid].set_rate(0)

    def local_failover(self, primary, backup):
        p, b = self.links[primary].snapshot(), self.links[backup].snapshot()
        chosen = primary if p['effective_up'] else (backup if b['effective_up'] else None)
        if chosen is None:
            return
        for (nid, dst), (peer, lid) in self.initial_routes.items():
            if lid == primary and self.state.node_online[nid]:
                if self.routes[nid, dst] != (peer, chosen):
                    self.routes[nid, dst] = (peer, chosen)
                    self.log('local-failover', dict(event='local_port_failover', time_ns=self.now,
                        node_id=nid, destination_node_id=dst, link_id=chosen))

    def set_local_rate_multiplier(self, nid, factor):
        """分布式学习智能体只调本节点源速率，其他节点资源不受本地动作直接修改。"""
        if self.config['mode'] != 'marl' or nid not in self.local_multipliers:
            raise ValueError('本地学习动作需要marl模式及合法节点')
        if type(factor) not in (int, float) or factor not in RATE_FACTORS:
            raise ValueError('本地速率动作倍率限定为0、0.5、1')
        self.local_multipliers[nid] = factor
        for sid, source in self.sources.items():
            if source.service.src_node_id == nid:
                rate = round(source.service.required_bps * self.config['rate_scale'] * factor) if source.active else 0
                if source.rate_bps != rate:
                    source.set_rate(rate)

    def advance(self, until_ns):
        if self._closed:
            raise RuntimeError('仿真实例已关闭')
        if type(until_ns) is not int:
            raise ValueError('推进终点必须使用整数纳秒')
        if until_ns > self.stop_ns:
            raise ValueError('超过配置的仿真结束时刻')
        advance_inclusive(self.env, until_ns)
        self._accumulate_demand(self.now)

    def summary(self):
        pending = Counter(p.kind for p in self.pending.values())
        conservation = {}
        for kind, row in self.metrics.totals.items():
            remaining = pending[kind]
            conservation[kind] = dict(generated=row['generated_packets'], delivered=row['delivered_packets'],
                dropped=row['dropped_packets'], pending=remaining,
                passed=row['generated_packets'] == row['delivered_packets'] + row['dropped_packets'] + remaining)
        # 逻辑账本与每条链路的实际队列/发送/传播数量相互核对。
        link_pending = sum(link.pending_count for link in self.links.values())
        result = dict(schema='resnet-python-result-v1', engine='SimPy', simulation_time_ns=self.now,
            node_count=len(self.state.node_online), link_count=len(self.links), batch_count=self.batch_count,
            core_event_counts=dict(self.event_count), config=self.config,
            totals={k: self.metrics.describe(v) for k, v in self.metrics.totals.items()},
            services={sid: self.metrics.describe(row) for sid, row in self.metrics.service_stats.items()},
            conservation=conservation, link_pending_packets=link_pending,
            pending_matches_links=link_pending == len(self.pending),
            final_state=self.state.snapshot(self.links),
            positions={nid: self.position(nid) for nid in self.state.node_online},
            demand_bits_by_service=self.demand_bits_by_service,
            links={lid: link.snapshot() for lid, link in self.links.items()},
            source_rates_bps={sid: source.rate_bps for sid, source in self.sources.items()},
            control_message_counts=dict(self.metrics.control_kinds),
            model=dict(capacity='bidirectional_shared_per_link', queue='finite_waiting_fifo',
                wire_overhead_bytes=28, controlled_epoch_overhead_bytes=8,
                loss='independent_packet_loss_ber_metadata',
                parameters='snapshot_at_serialization_start',
                window='continuous' if self.start_ns == 0 else 'cold_start_no_prior_packets',
                same_timestamp='scene_batch_before_network_events',
                output_boundary='inclusive', ip_mac_phy='abstracted',
                training='interface_and_baselines_only'))
        if self.control:
            result['control'] = self.control.summary()
        result['checks_passed'] = all(v['passed'] for v in conservation.values()) and result['pending_matches_links']
        return result

    def close(self):
        for handle in self._files.values():
            handle.close()
        self._closed = True
