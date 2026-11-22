"""按仿真时钟采样的只读记录器，供事件时间轴分析。

输入：已装配的 Simulation、输出文件、采样间隔；输出：逐时刻 JSONL。
每个累计计数来自实际收发；相邻样本作差后可得到该时间段的吞吐和丢包。
队列长度是采样瞬间的快照，不能声称捕获了两个采样点之间的最高峰。
采样不调用策略、不消费随机数、不修改队列，优先级 2 使同刻网络事件先执行。
"""
from pathlib import Path
import json
import math


COUNTERS = ('generated_packets', 'delivered_packets', 'dropped_packets',
            'generated_payload_bytes', 'delivered_payload_bytes', 'generated_wire_bytes',
            'deadline_success_packets', 'deadline_success_payload_bytes', 'delay_sum_ns')


class TelemetryRecorder:
    def __init__(self, simulation, path, interval_s=1):
        if type(interval_s) not in (int, float) or not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError('采样间隔必须为有限正数')
        self.interval_ns = round(interval_s * 1e9)
        if self.interval_ns < 1:
            raise ValueError('采样间隔不能小于1纳秒')
        self.sim = simulation
        self.closed = False
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('x', encoding='utf-8')
        self.timer = simulation.schedule(simulation.now, self.sample, priority=2)

    @staticmethod
    def counters(row):
        # 复制累计计数，避免之后的网络变化改写已经采集的样本。
        return {key: row[key] for key in COUNTERS} | {'drop_reasons': dict(row['drop_reasons'])}

    def sample(self):
        if self.closed:
            return
        sim = self.sim
        links = {}
        for lid, link in sim.links.items():
            snapshot = link.snapshot()
            links[lid] = {key: snapshot[key] for key in (
                'capacity_bps', 'delay_ms', 'jitter_ms', 'packet_loss_rate', 'ber',
                'admin_up', 'effective_up', 'queue_packets', 'queued_bytes',
                'queue_peak_packets', 'queue_wait_count', 'queue_wait_sum_ns',
                'queue_wait_max_ns', 'busy_ns', 'tx_started', 'tx_finished', 'rx',
                'serialized_wire_bytes', 'delivered_wire_bytes', 'drops', 'drop_reasons',
                'pending_count', 'in_flight')}
        active = sorted(sim.state.active)
        row = dict(time_ns=sim.now, profile=sim.state.profile,
            active_service_ids=active, online_nodes=sum(sim.state.node_online.values()),
            available_links=sum(link.effective_up for link in sim.links.values()),
            requested_bps=sum(sim.services[sid].required_bps for sid in active),
            realtime_requested_bps=sum(sim.services[sid].required_bps for sid in active
                                      if sim.services[sid].service_type in ('voice', 'video')),
            source_rates_bps={sid: source.rate_bps for sid, source in sim.sources.items()},
            totals={kind: self.counters(stats) for kind, stats in sim.metrics.totals.items()},
            services={sid: self.counters(stats) for sid, stats in sim.metrics.service_stats.items()},
            links=links)
        self.handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
        # 事件时刻是模型时间；不会让程序在现实世界等待 interval_s 秒。
        next_ns = min(sim.now + self.interval_ns, sim.stop_ns)
        if next_ns > sim.now:
            self.timer = sim.schedule(next_ns, self.sample, priority=2)

    def close(self):
        if not self.closed:
            self.closed = True
            self.timer.cancel()
            self.handle.close()
