"""基于 SimPy 的双向共享容量有限 FIFO 链路。

一条链路有一个等候区和一个发送位置。两端发来的包排同一条队；
发送位置按“包的比特数 / 每秒容量”工作。队伍满了就丢包，传播不占发送位置。

输入：SimPy 环境、两端节点、五参数、队列上限、种子和三个回调。
输出：on_arrival(packet, peer, link_id) 或 on_drop(packet, reason, link_id)，
以及 snapshot() 返回的统计字典。回调表示这一跳的实际结果，不能预先算收包。
阅读顺序：put → _start_next → _finish → _arrive，再看状态变化和 purge。
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math
import random
from typing import Callable

from simpy.events import Event, NORMAL

from .packet import Packet


@dataclass(slots=True)
class _Job:
    """一个包在本链路的一次传输；同一个包可在后续链路建立另一个 Job。"""

    packet: Packet
    from_node_id: str
    enqueue_ns: int
    tx_id: int
    parameters: dict
    generation: int
    start_ns: int = -1
    serialization_ns: int = 0
    propagation_ns: int = 0
    loss_sample: float = 0.0
    done: bool = False


class ResourceLink:
    """一条独立资源边；平行边必须使用不同实例和 link_id。

    queue_packets/queue_bytes 只约束等待区，不包含正在发送的一个包及传播中的包。
    本模型使用双向共享容量；它是项目的资源模型，并非所有现实无线设备的通用属性。
    """

    PARAMETER_NAMES = {"capacity_bps", "delay_ms", "jitter_ms", "packet_loss_rate", "ber"}

    def __init__(self, env, link_id, endpoints, parameters, queue_packets,
                 queue_bytes, seed, on_arrival, on_drop, on_trace=None):
        if len(endpoints) != 2 or endpoints[0] == endpoints[1]:
            raise ValueError("链路需要两个不同端点")
        if not all(isinstance(n, str) and n for n in endpoints):
            raise ValueError("节点编号必须是非空字符串")
        if not isinstance(link_id, str) or not link_id:
            raise ValueError("link_id 必须是非空字符串")
        for name, value in (("queue_packets", queue_packets), ("queue_bytes", queue_bytes)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数，0 不代表无限队列")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed 必须为非负整数")
        self.env, self.link_id = env, link_id
        self.endpoints = tuple(endpoints)
        self._parameters = self._validated(dict(parameters))
        self.max_queue_packets, self.max_queue_bytes = queue_packets, queue_bytes
        self.on_arrival, self.on_drop, self.on_trace = on_arrival, on_drop, on_trace
        self.admin_up = True
        self.endpoint_online = {node: True for node in self.endpoints}
        self._queue = deque()
        self.queued_bytes = 0
        self._current = None
        self._in_flight = 0
        self._generation = 0
        self._next_id = 0
        # 独立随机流：改变时延抽样次数不会顺带改变丢包抽样序列。
        self._delay_rng = random.Random(seed * 2)
        self._loss_rng = random.Random(seed * 2 + 1)
        self._counts = Counter()
        self._drop_reasons = Counter()
        self._queue_wait_sum_ns = 0
        self._queue_wait_max_ns = 0
        self._queue_peak_packets = 0
        self._queue_peak_bytes = 0
        self._busy_completed_ns = 0
        self._trace("configure", reason="initial")

    @classmethod
    def _validated(cls, params):
        """整组校验后才生效；失败的更新不会留下半套新参数。"""
        if set(params) != cls.PARAMETER_NAMES:
            raise ValueError(f"链路参数必须恰好包含 {sorted(cls.PARAMETER_NAMES)}")
        if type(params["capacity_bps"]) is not int or params["capacity_bps"] <= 0:
            raise ValueError("capacity_bps 必须为正整数；断链请使用 set_up(False)")
        for key in cls.PARAMETER_NAMES - {"capacity_bps"}:
            value = params[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} 必须是有限数值")
            if value < 0 or (key in ("packet_loss_rate", "ber") and value > 1):
                raise ValueError(f"{key} 超出允许范围")
        if params["jitter_ms"] > 0 and params["delay_ms"] <= 0:
            raise ValueError("Gamma 时延模型在 jitter_ms > 0 时要求 delay_ms > 0")
        return params

    @property
    def parameters(self):
        """只返回参数副本；运行时修改必须经过 update_parameters 的校验。"""
        return dict(self._parameters)

    @property
    def effective_up(self):
        """链路自身可用，且两端节点都在线，才可以开始服务。"""
        return self.admin_up and all(self.endpoint_online.values())

    @property
    def busy(self):
        return self._current is not None

    @property
    def pending_count(self):
        """尚未在这一跳完成交付或丢弃的包数；故障后尚在传播的包也保留。"""
        return len(self._queue) + int(self.busy) + self._in_flight

    def put(self, packet: Packet, from_node_id: str) -> bool:
        """尝试入队；返回是否入队，不代表最终送达。

        SimPy Store 满时默认等待；本模型必须主动检查并丢弃，不能把源端静默阻塞。
        空闲时立即取队首开始发送，避免同一时刻的多个到包多占一个等待位置。
        """
        if from_node_id not in self.endpoint_online:
            raise ValueError("发送节点不属于这条链路")
        if type(packet.wire_bytes) is not int or packet.wire_bytes <= 0:
            raise ValueError("wire_bytes 必须为正整数")
        self._next_id += 1
        job = _Job(packet, from_node_id, int(self.env.now), self._next_id,
                   self._parameters, self._generation)
        self._counts["attempts"] += 1
        if not self.endpoint_online[from_node_id]:
            self._drop(job, "node_offline_sender")
            return False
        packets_full = len(self._queue) >= self.max_queue_packets
        bytes_full = self.queued_bytes + packet.wire_bytes > self.max_queue_bytes
        if packets_full or bytes_full:
            self._drop(job, "queue_full")
            return False
        self._queue.append(job)
        self.queued_bytes += packet.wire_bytes
        self._counts["enqueued"] += 1
        self._queue_peak_packets = max(self._queue_peak_packets, len(self._queue))
        self._queue_peak_bytes = max(self._queue_peak_bytes, self.queued_bytes)
        self._trace("enqueue", job)
        self._start_next()
        return True

    def _later(self, delay_ns: int, callback):
        """向 SimPy 登记完成事件；轻量 Event 避免每包额外创建多个 Process。

        事件由 env.step/run 推进。_ok/_value 是 SimPy Event.succeed 同样设置的
        完成状态；使用 env.schedule 保留整数纳秒以及标准 NORMAL 事件优先级。
        """
        event = Event(self.env)
        event._ok, event._value = True, None
        event.callbacks.append(lambda _: callback())
        self.env.schedule(event, priority=NORMAL, delay=delay_ns)

    def _start_next(self):
        if self.busy or not self.effective_up or not self._queue:
            return
        job = self._queue.popleft()
        self.queued_bytes -= job.packet.wire_bytes
        self._current = job
        # 参数更新采用替换字典，所以旧对象自然成为本包不可变的参数快照。
        job.parameters = self._parameters
        job.generation = self._generation
        job.start_ns = int(self.env.now)
        capacity = job.parameters["capacity_bps"]
        # 整数向上取整：避免时间舍入导致容量高估。
        job.serialization_ns = (job.packet.wire_bytes * 8 * 1_000_000_000 + capacity - 1) // capacity
        delay_ms, jitter_ms = job.parameters["delay_ms"], job.parameters["jitter_ms"]
        if jitter_ms:
            shape = (delay_ms / jitter_ms) ** 2
            scale = jitter_ms ** 2 / delay_ms
            if not math.isfinite(shape) or not math.isfinite(scale) or shape <= 0 or scale <= 0:
                raise ValueError("Gamma 参数数值范围不可表示")
            delay_ms = self._delay_rng.gammavariate(shape, scale)
        if not math.isfinite(delay_ms) or delay_ms < 0:
            raise ValueError("传播时延抽样无效")
        job.propagation_ns = int(math.floor(delay_ms * 1_000_000 + 0.5))
        job.loss_sample = self._loss_rng.random()
        waiting = job.start_ns - job.enqueue_ns
        self._queue_wait_sum_ns += waiting
        self._queue_wait_max_ns = max(self._queue_wait_max_ns, waiting)
        self._counts["tx_started"] += 1
        self._counts["started_wire_bytes"] += job.packet.wire_bytes
        self._trace("start", job)
        self._later(job.serialization_ns, lambda: self._finish(job))

    def _finish(self, job):
        # 故障或版本清理已经丢弃的任务，其旧完成事件只能成为无操作。
        if job.done or self._current is not job:
            return
        self._busy_completed_ns += job.serialization_ns
        self._counts["tx_finished"] += 1
        self._counts["serialized_wire_bytes"] += job.packet.wire_bytes
        self._current = None
        self._in_flight += 1
        self._trace("finish", job)
        self._later(job.propagation_ns, lambda: self._arrive(job))
        # 传播与下一包的发送可以重叠；不能在这里再等传播完成。
        self._start_next()

    def _arrive(self, job):
        self._in_flight -= 1
        if job.generation != self._generation or not self.effective_up:
            self._drop(job, "stale_generation")
            return
        if job.loss_sample < job.parameters["packet_loss_rate"]:
            self._drop(job, "packet_loss")
            return
        job.done = True
        self._counts["rx"] += 1
        self._counts["delivered_wire_bytes"] += job.packet.wire_bytes
        self._trace("deliver", job)
        peer = self.endpoints[1] if job.from_node_id == self.endpoints[0] else self.endpoints[0]
        self.on_arrival(job.packet, peer, self.link_id)

    def _drop(self, job, reason):
        if job.done:
            return
        job.done = True
        self._counts["drops"] += 1
        self._drop_reasons[reason] += 1
        self._trace("drop", job, reason)
        self.on_drop(job.packet, reason, self.link_id)

    def update_parameters(self, **params):
        """部分参数更新；仅影响之后开始服务的包，已开始的包沿用旧快照。"""
        self._parameters = self._validated(self._parameters | params)
        self._trace("update", reason="parameters")

    def _interrupt_current(self, reason):
        if self._current is None:
            return
        job = self._current
        self._busy_completed_ns += int(self.env.now) - job.start_ns
        self._current = None
        self._drop(job, reason)

    def set_up(self, up: bool):
        if type(up) is not bool:
            raise ValueError("up 必须为 bool")
        if up == self.admin_up:
            return
        self.admin_up = up
        if not up:
            self._generation += 1
            self._trace("down", reason="link_state")
            self._interrupt_current("link_down_during_serialization")
        else:
            self._trace("up", reason="link_state")
            self._start_next()

    def set_endpoint_online(self, node_id: str, online: bool):
        if node_id not in self.endpoint_online or type(online) is not bool:
            raise ValueError("需要有效端点编号及 bool 在线状态")
        if self.endpoint_online[node_id] == online:
            return
        self.endpoint_online[node_id] = online
        if not online:
            self._generation += 1
            self._trace("endpoint_offline", reason=node_id)
            self._interrupt_current("node_offline_during_serialization")
            self.purge(lambda packet, sender: sender == node_id, "node_offline_local_queue")
        else:
            self._trace("endpoint_online", reason=node_id)
            self._start_next()

    def purge(self, predicate: Callable[[Packet, str], bool], reason="epoch_cleanup") -> int:
        """清理本地等待与发送任务；predicate(packet, from_node_id) 决定匹配。

        传播包已离开发送端，不在此处回收；下一节点的版本规则应检查其合法性。
        本操作不增加故障代次，不干扰其它业务或相反方向的合法在途包。
        """
        removed = 0
        keep, rejected = deque(), []
        for job in self._queue:
            if predicate(job.packet, job.from_node_id):
                rejected.append(job)
                self.queued_bytes -= job.packet.wire_bytes
            else:
                keep.append(job)
        self._queue = keep
        for job in rejected:
            self._drop(job, reason)
            removed += 1
        if self._current is not None and predicate(self._current.packet, self._current.from_node_id):
            self._interrupt_current(reason)
            removed += 1
        self._start_next()
        return removed

    def snapshot(self):
        """当前状态及累计统计；所有量都可直接写入 JSON。

        queue_wait_count/sum/max 只统计已经开始服务的任务；正在等待的包尚无最终
        等待时长。busy_ns 包含发送中断已耗时间及当前发送已耗时间，不含传播。
        """
        counts = {key: self._counts[key] for key in (
            "attempts", "enqueued", "tx_started", "tx_finished", "rx", "drops",
            "started_wire_bytes", "serialized_wire_bytes", "delivered_wire_bytes")}
        elapsed = int(self.env.now) - self._current.start_ns if self._current else 0
        return dict(self._parameters) | {
            "link_id": self.link_id, "endpoints": list(self.endpoints),
            "parameters": dict(self._parameters), "admin_up": self.admin_up,
            "effective_up": self.effective_up, "endpoint_online": dict(self.endpoint_online),
            "generation": self._generation, "queue_packets": len(self._queue),
            "queued_bytes": self.queued_bytes, "queue_limit_packets": self.max_queue_packets,
            "queue_limit_bytes": self.max_queue_bytes, "busy": self.busy,
            "in_flight": self._in_flight, "pending_count": self.pending_count,
            "queue_peak_packets": self._queue_peak_packets, "queue_peak_bytes": self._queue_peak_bytes,
            "queue_wait_count": self._counts["tx_started"],
            "queue_wait_sum_ns": self._queue_wait_sum_ns, "queue_wait_max_ns": self._queue_wait_max_ns,
            "busy_ns": self._busy_completed_ns + elapsed, "drop_reasons": dict(self._drop_reasons),
        } | counts

    def _trace(self, event, job=None, reason=""):
        # 关闭逐包跟踪时避免创建字典；累计统计仍然完整计数。
        if self.on_trace is None:
            return
        row = {"event": event, "time_ns": int(self.env.now), "link_id": self.link_id,
               "reason": reason, "queue_packets": len(self._queue), "queued_bytes": self.queued_bytes,
               "generation": self._generation}
        if job is not None:
            row.update(packet_uid=job.packet.uid, service_id=job.packet.service_id, kind=job.packet.kind,
                       epoch=job.packet.epoch, tx_id=job.tx_id, from_node_id=job.from_node_id,
                       wire_bytes=job.packet.wire_bytes, enqueue_ns=job.enqueue_ns, start_ns=job.start_ns,
                       serialization_ns=job.serialization_ns, propagation_ns=job.propagation_ns,
                       queue_wait_ns=job.start_ns-job.enqueue_ns if job.start_ns >= 0 else None,
                       packet_generation=job.generation, capacity_bps=job.parameters["capacity_bps"])
        self.on_trace(row)
