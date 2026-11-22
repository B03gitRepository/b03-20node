"""节点与业务安排的状态记录，以及只读的网络状态快照。

SceneState 保存节点在线状态、采样位置、业务登记和活动安排。
链路当前参数与通断只保存在 ResourceLink；snapshot 按需读取链路并汇总。
事件统一由 Simulation._apply_event 执行，本模块不再解释事件或安排传输。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

from resnet.contracts.scenario import ScenarioSpec

if TYPE_CHECKING:
    from .link import ResourceLink


@dataclass
class SceneState:
    """场景运行状态；snapshot 返回可保存为 JSON 的状态快照。"""

    node_online: dict[str, bool]
    positions: dict[str, dict]
    registered: set[str]
    active: set[str] = field(default_factory=set)
    profile: str | None = None

    @classmethod
    def from_scenario(cls, scenario: ScenarioSpec) -> "SceneState":
        """复制初始节点和业务安排；运行中的事件由 Simulation 统一写入。"""
        return cls(
            node_online={n.node_id: True for n in scenario.nodes},
            positions={n.node_id: dict(n.position) for n in scenario.nodes},
            registered={s.service_id for s in scenario.services if s.registered_at_us == 0},
        )

    def snapshot(self, links: Mapping[str, ResourceLink]) -> dict:
        """返回独立副本，后面的变化不会修改已经保存的历史快照。

        links 由 Simulation 传入；只读取链路当前参数和通断，不保留第二份运行状态。
        返回字段沿用原有日志和报告格式，包括 link_params、link_admin_up。
        positions 表示脚本采样位置；连续运动位置由 Simulation.position 插值计算。
        admin_up 是链路自身的通断，effective_up 还要求两端节点在线。
        """
        return deepcopy({
            "node_online": self.node_online,
            "link_admin_up": {lid: link.admin_up for lid, link in links.items()},
            "link_effective_up": {lid: link.effective_up for lid, link in links.items()},
            "link_params": {lid: link.parameters for lid, link in links.items()},
            "positions": self.positions,
            "registered_service_ids": sorted(self.registered),
            "active_service_ids": sorted(self.active), "profile": self.profile,
        })

def service_is_active(state: SceneState, service_id: str, *, in_window: bool,
                      selected_ids: list[str] | None) -> bool:
    """读取场景安排，判断业务是否在本轮有效活动范围内，不修改任何对象。

    调用方传入窗口判定；本函数统一登记、活动和业务筛选条件。
    节点离线和实际发送速率由业务源处理，暂时不能发送不会消除业务需求。
    selected_ids=None 表示不额外筛选业务。
    """
    return (in_window and service_id in state.active
            and service_id in state.registered
            and (selected_ids is None or service_id in selected_ids))
