"""资源决策函数接口与返回值校验。

替换方式：实现与 DecisionPolicy 相同签名的函数，通过
Simulation(..., decision_policy=函数) 或 run_case(..., decision_policy=函数) 注入。
默认使用 policy.decide。函数只获得已送达的观测、当前业务需求与保留预算；
不获得 Simulation、场景脚本、未来事件或远端节点句柄。

返回值保持普通字典，便于后续接入启发式算法或资源管控智能体：
  {"decided_at_ns": now_ns, "decisions": [{
      "service_id": ..., "accepted": bool, "reason": str,
      "node_path": [...], "link_path": [...],
      "reserved_bps": int, "wire_bps": int, ...}]}
accepted=False 时至少提供 service_id、accepted 和 reason；其余字段可用于诊断。
accepted=True 时必须提供完整路径和预留速率。协调器当前一次传入一项业务，
因此也只接收该业务的一条建议。结果通过校验后仍需发送、执行和确认控制命令。
"""
from collections.abc import Sequence
from typing import Protocol

from resnet.contracts.control import require_int
from resnet.contracts.scenario import ServiceSpec
from .policy import required_wire_bps


class DecisionPolicy(Protocol):
    """可调用对象协议；实现函数或带 __call__ 的类均可。"""

    def __call__(self, snapshot: dict, services: Sequence[ServiceSpec], *,
                 now_ns: int, payload_bytes: int,
                 epoch_overhead_bytes: int, control_headroom_fraction: float,
                 retained_wire_bps: dict[str, int]) -> dict:
        """根据当前缓存生成路径和速率建议，不直接更改网络。"""
        ...


def validate_policy_result(result, service, snapshot, *, now_ns):
    """拒绝业务错配、未知链路和格式错误；不替代目标节点的时效/版本检查。

路径必须来自本轮有效观测。接纳规则和 QoS 权衡由策略决定；容量超配造成的
排队和丢包仍由网络模型呈现。wire_bps 必须按实际分组开销计入事务预算。
"""
    if not isinstance(result, dict):
        raise ValueError('决策结果必须是字典')
    decided_at_ns = require_int(result.get('decided_at_ns'), 'decided_at_ns')
    if decided_at_ns != now_ns:
        raise ValueError('决策结果必须包含当前时刻的 decided_at_ns')
    decisions = result.get('decisions')
    if not isinstance(decisions, list) or len(decisions) != 1:
        raise ValueError('当前协调器每次需要一项业务的一条决策')
    choice = decisions[0]
    if (not isinstance(choice, dict) or choice.get('service_id') != service.service_id
            or type(choice.get('accepted')) is not bool
            or not isinstance(choice.get('reason'), str) or not choice['reason']):
        raise ValueError('决策应包含匹配的 service_id、布尔 accepted 和非空 reason')
    if not choice['accepted']:
        return
    nodes, links = choice.get('node_path'), choice.get('link_path')
    if (not isinstance(nodes, list) or not isinstance(links, list)
            or len(nodes) != len(links) + 1 or not links
            or any(not isinstance(node, str) for node in nodes)
            or any(not isinstance(link, str) for link in links)
            or len(set(nodes)) != len(nodes)
            or nodes[0] != service.src_node_id or nodes[-1] != service.dst_node_id):
        raise ValueError('接纳路径必须从业务源到目的节点，且逐跳对应、无环')
    for source, target, link_id in zip(nodes, nodes[1:], links):
        row = snapshot['links'].get(link_id)
        if (row is None or not row['usable']
                or {source, target} != {row['src_node_id'], row['dst_node_id']}):
            raise ValueError('策略路径包含未知、失效或端点不匹配的链路')
    rate = require_int(choice.get('reserved_bps'), 'reserved_bps', 1)
    wire = require_int(choice.get('wire_bps'), 'wire_bps', 1)
    # control 模式沿用 1000 B 业务负载与 8 B 版本字段；仿真配置会验证这一约束。
    expected_wire = required_wire_bps(rate, payload_bytes=1000, epoch_overhead_bytes=8)
    if wire != expected_wire:
        raise ValueError('wire_bps 须包含 1000 B 负载、8 B 版本字段与 28 B 协议开销')
