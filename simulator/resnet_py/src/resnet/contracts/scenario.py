"""场景的 Python 数据结构；raw 字段保留来源，不作为运行状态。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    node_type: str
    official_ip: str
    mask: str
    position: dict[str, float]
    raw: dict[str, Any]


@dataclass(frozen=True)
class LinkSpec:
    link_id: str
    link_type: str
    src_node_id: str
    dst_node_id: str
    capacity_bps: int
    delay_s: float
    jitter_s: float
    ber: float
    packet_loss_rate: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    service_type: str
    category: str | None
    src_node_id: str
    dst_node_id: str
    priority: int
    required_bps: int
    delay_limit_s: float
    jitter_limit_s: float
    ber_limit: float
    loss_limit: float
    registered_at_us: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    event_type: str
    time_us: int
    sequence: int
    attrs: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class EventBatch:
    time_us: int
    sequence: int
    operations: tuple[dict[str, Any], ...]
    profile: str | None
    active_service_ids: tuple[str, ...]

    @property
    def time_s(self) -> float:
        """只在展示时转换为秒；批次排序始终使用整数微秒。"""
        return self.time_us / 1_000_000


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    duration_s: float
    nodes: tuple[NodeSpec, ...]
    links: tuple[LinkSpec, ...]
    services: tuple[ServiceSpec, ...]
    added_services: tuple[ServiceSpec, ...]
    events: tuple[EventSpec, ...]
    batches: tuple[EventBatch, ...]
    warnings: tuple[str, ...]
    source_hashes: dict[str, str]
    raw_documents: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        """返回可直接编码为 JSON 的输入摘要；不包含任何仿真测量结果。"""
        return {
            "scenario_id": self.scenario_id,
            "duration_s": self.duration_s,
            "nodes": len(self.nodes),
            "links": len(self.links),
            "initial_services": len(self.services),
            "added_services": len(self.added_services),
            "core_events": len(self.events),
            "batches": len(self.batches),
            "event_types": sorted({e.event_type for e in self.events}),
            "warnings": list(self.warnings),
            "source_hashes": dict(self.source_hashes),
        }
