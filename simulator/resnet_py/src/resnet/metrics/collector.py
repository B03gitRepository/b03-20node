"""在线统计：根据分组生成、交付和丢弃事件计算实测指标。

输入 Packet 的生成、交付、终结丢弃事件；输出按业务与按秒汇总的普通字典。
生成包只会有一个最终结局；截止时刻尚在网络中的包另计 pending。不会把每一包
永久保存在内存。详细样本与控制消息由 Simulation 的日志模块写出。
"""
from collections import Counter


def empty_stats():
    return dict(generated_packets=0, delivered_packets=0, dropped_packets=0,
                generated_payload_bytes=0, delivered_payload_bytes=0,
                generated_wire_bytes=0, deadline_success_payload_bytes=0,
                deadline_success_packets=0, delay_sum_ns=0, delay_max_ns=0,
                delay_min_ns=None, abs_delay_difference_sum_ns=0,
                previous_delay_ns=None, drop_reasons={})


class Collector:
    def __init__(self, services):
        self.services = services
        self.service_stats = {sid: empty_stats() for sid in services}
        self.totals = {'data': empty_stats(), 'control': empty_stats()}
        self.timeseries = {}
        self.control_kinds = Counter()

    def _targets(self, packet):
        yield self.totals[packet.kind]
        if packet.kind == 'data':
            yield self.service_stats[packet.service_id]

    def _bucket(self, now):
        second = now // 1_000_000_000
        if second not in self.timeseries:
            self.timeseries[second] = dict(time_s=second, generated_packets=0,
                delivered_packets=0, dropped_packets=0, delivered_payload_bytes=0,
                delay_sum_ns=0, control_generated_packets=0, control_delivered_packets=0)
        return self.timeseries[second]

    def generated(self, packet, now):
        for row in self._targets(packet):
            row['generated_packets'] += 1
            row['generated_payload_bytes'] += packet.payload_bytes
            row['generated_wire_bytes'] += packet.wire_bytes
        key = 'generated_packets' if packet.kind == 'data' else 'control_generated_packets'
        self._bucket(now)[key] += 1
        if packet.kind == 'control':
            self.control_kinds['tx_' + packet.message['kind']] += 1

    def delivered(self, packet, now):
        delay = now - packet.created_at_ns
        meets = packet.kind == 'data' and delay <= round(self.services[packet.service_id].delay_limit_s * 1e9)
        for row in self._targets(packet):
            row['delivered_packets'] += 1
            row['delivered_payload_bytes'] += packet.payload_bytes
            row['delay_sum_ns'] += delay
            row['delay_max_ns'] = max(row['delay_max_ns'], delay)
            row['delay_min_ns'] = delay if row['delay_min_ns'] is None else min(row['delay_min_ns'], delay)
            if row['previous_delay_ns'] is not None:
                row['abs_delay_difference_sum_ns'] += abs(delay - row['previous_delay_ns'])
            row['previous_delay_ns'] = delay
            if meets:
                row['deadline_success_payload_bytes'] += packet.payload_bytes
                row['deadline_success_packets'] += 1
        bucket = self._bucket(now)
        if packet.kind == 'data':
            bucket['delivered_packets'] += 1
            bucket['delivered_payload_bytes'] += packet.payload_bytes
            bucket['delay_sum_ns'] += delay
        else:
            bucket['control_delivered_packets'] += 1
            self.control_kinds['rx_' + packet.message['kind']] += 1

    def dropped(self, packet, now, reason):
        for row in self._targets(packet):
            row['dropped_packets'] += 1
            reasons = row['drop_reasons']
            reasons[reason] = reasons.get(reason, 0) + 1
        if packet.kind == 'data':
            self._bucket(now)['dropped_packets'] += 1

    @staticmethod
    def describe(row):
        result = {k: v for k, v in row.items() if k != 'previous_delay_ns'}
        result['mean_delay_ms'] = row['delay_sum_ns'] / row['delivered_packets'] / 1e6 if row['delivered_packets'] else None
        result['delivery_fraction_of_generated'] = row['delivered_packets'] / row['generated_packets'] if row['generated_packets'] else None
        return result
