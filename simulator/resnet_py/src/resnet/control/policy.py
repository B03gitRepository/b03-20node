"""普通确定性规则：优先级排序、共享容量约束、最小时延路径。

先看已经收到的有效报告，再让优先级高的业务先选能放得下的路。
输出只是配置建议，不直接改节点；真正安装、切换和回执由带内执行流程完成。
输入：观测快照、ServiceSpec列表、包开销和保留预算；输出：逐业务路径及原因。
依赖：已校验的观测快照和业务需求；不读取场景未来事件或全局真实状态。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import heapq
import math

from resnet.contracts.control import require_int


def required_wire_bps(payload_bps: int, payload_bytes: int = 1000,
                      epoch_overhead_bytes: int = 8) -> int:
    """将业务负载速率折算为链路计费速率，整数向上取整，含epoch和IPv4/UDP头。"""
    require_int(payload_bps, "payload_bps")
    require_int(payload_bytes, "payload_bytes", 1)
    require_int(epoch_overhead_bytes, "epoch_overhead_bytes")
    numerator = payload_bps * (payload_bytes + epoch_overhead_bytes + 28)
    return (numerator + payload_bytes - 1) // payload_bytes


def _path(links: dict, source: str, destination: str, residual: dict | None,
          demand: int, *, include_unusable: bool = False):
    """多重图Dijkstra；平局依次按跳数和linkId序列，不合并并行链路。"""
    graph = {}
    for lid, link in sorted(links.items()):
        if not include_unusable and not link["usable"]:
            continue
        if residual is not None and residual.get(lid, 0) < demand:
            continue
        a, b = link["src_node_id"], link["dst_node_id"]
        # include_unusable仅用于判断静态拓扑是否相连；缺观测不虚构质量供正式选路。
        weight = link.get("delay_ns", 0) if not include_unusable else 0
        graph.setdefault(a, []).append((b, lid, weight))
        graph.setdefault(b, []).append((a, lid, weight))
    best = {source: (0, 0, ())}
    pending = [(0, 0, (), source, (source,))]
    while pending:
        delay, hops, lids, current, nodes = heapq.heappop(pending)
        if best.get(current) != (delay, hops, lids):
            continue
        if current == destination:
            return list(nodes), list(lids)
        for peer, lid, weight in sorted(graph.get(current, [])):
            if peer in nodes:
                continue  # 显式禁止回到已访问节点，即使零时延边也不允许环。
            candidate = (delay + weight, hops + 1, lids + (lid,))
            if peer not in best or candidate < best[peer]:
                best[peer] = candidate
                heapq.heappush(pending, (*candidate, peer, nodes + (peer,)))
    return None


def _quality(service, path_links: list[str], links: dict, wire_bytes: int,
             *, qos_profile: str = 'baseline') -> tuple[dict, list[str]]:
    """计算配置级质量；不预测排队，也不把配置 BER 称为实测误码率。

    official 对应官方 sce01-stats.md 的路径合成公式：D=sum(delay)，
    J=sqrt(sum(jitter²))，L=1-prod(1-loss)，BER≈sum(BER)。传输仍按
    分组大小、容量及队列另行计算，因此实际时延可能超过配置级估计。
    """
    edges = [links[lid] for lid in path_links]
    propagation_s = sum(e["delay_ns"] for e in edges) / 1e9
    serialization_s = sum(wire_bytes * 8 / e["capacity_bps"] for e in edges)
    mean_delay_s = propagation_s + serialization_s
    jitter_s = math.sqrt(sum((e["jitter_ns"] / 1e9) ** 2 for e in edges))
    loss = 1 - math.prod(1 - e["packet_loss_rate"] for e in edges)
    max_ber = max((e["ber"] for e in edges), default=0)
    estimates = {"configured_propagation_s": propagation_s, "serialization_s": serialization_s,
                 "mean_delay_without_queue_s": mean_delay_s,
                 "independent_jitter_std_s": jitter_s, "independent_path_loss": loss,
                 "max_configured_ber_metadata": max_ber,
                 "scope": "configuration_estimate_not_measured_qos"}
    checked_delay_s, checked_ber = mean_delay_s, max_ber
    if qos_profile == 'official':
        # 官方公式中的 d_i 是链路配置时延；串行发送与排队耗时另列。
        checked_delay_s = propagation_s
        checked_ber = sum(e['ber'] for e in edges)
        estimates.update(
            qos_profile='official', configured_path_bandwidth_bps=min(
                (e['capacity_bps'] for e in edges), default=0),
            configured_delay_sum_s=propagation_s,
            configured_ber_sum=checked_ber,
            admission_scope='selected_candidate_configuration_qos_only',
        )
    violations = []
    for value, threshold, name in ((checked_delay_s, service.delay_limit_s, "delay_limit"),
                                    (jitter_s, service.jitter_limit_s, "jitter_limit"),
                                    (loss, service.loss_limit, "loss_limit"),
                                    (checked_ber, service.ber_limit, "ber_metadata_limit")):
        if value > threshold:
            violations.append(name)
    return estimates, violations


def decide(snapshot: dict, services, *, now_ns: int, payload_bytes: int = 1000,
           epoch_overhead_bytes: int = 8, control_headroom_fraction: float = .05,
           retained_wire_bps: dict[str, int] | None = None,
           qos_profile: str = 'baseline') -> dict:
    """生成确定性的按业务路径建议，不安装路由、不改变远端发送速率。

    snapshot必须刚在now_ns生成，缓存会再次检查双端新鲜度。retained_wire_bps
    是控制器仍需保留的旧epoch或未解除预留，跨两个方向共同扣除；不擅自把尚未
    确认清理的旧预算释放。业务自身采用完整需求接纳或拒绝，不自动降低需求速率。
    baseline 保留原有接纳规则，QoS超限作为结果记录。official 按官方公式
    检查已选出的最小时延候选，超限则拒绝，且不占用该候选的容量预算。
    该基线不搜索全部满足多约束的路径；拒绝某候选不代表不存在其他可行路径。
    """
    require_int(now_ns, "now_ns")
    require_int(payload_bytes, "payload_bytes", 1)
    require_int(epoch_overhead_bytes, "epoch_overhead_bytes")
    if not isinstance(qos_profile, str) or qos_profile not in ('baseline', 'official'):
        raise ValueError('qos_profile 必须为 baseline 或 official')
    if snapshot.get("sampled_for_decision_at_ns") != now_ns:
        raise ValueError("必须使用当前决策时刻重新生成的观测快照")
    if type(control_headroom_fraction) not in (int, float) or not math.isfinite(control_headroom_fraction) or not 0 <= control_headroom_fraction < 1:
        raise ValueError("control_headroom_fraction应为[0,1)有限数")
    fraction = Decimal(str(control_headroom_fraction))
    links = snapshot["links"]
    retained = dict(retained_wire_bps or {})
    if not set(retained) <= set(links):
        raise ValueError("保留预算引用未知链路")
    for lid, value in retained.items():
        require_int(value, f"retained_wire_bps[{lid}]")
    budgets = {lid: int((Decimal(row["capacity_bps"]) * (1-fraction)).to_integral_value(rounding=ROUND_FLOOR))
               if row["usable"] else 0 for lid, row in links.items()}
    remaining = {lid: max(0, budget - retained.get(lid, 0)) for lid, budget in budgets.items()}
    services = list(services)
    if len({s.service_id for s in services}) != len(services):
        raise ValueError("重复业务ID不能重复预留容量")
    decisions = []
    for service in sorted(services, key=lambda s: (-s.priority, s.service_id)):
        demand = required_wire_bps(service.required_bps, payload_bytes, epoch_overhead_bytes)
        source, target = service.src_node_id, service.dst_node_id
        if source == target:
            raise ValueError("网络策略不接受同节点业务")
        base = {"service_id": service.service_id, "priority": service.priority,
                "accepted": False, "reason": None, "node_path": [], "link_path": [],
                "reserved_bps": 0, "wire_bps": 0, "required_payload_bps": service.required_bps,
                "required_wire_bps": demand, "quality_estimates": None, "violations": []}
        found = _path(links, source, target, remaining, demand)
        if found is None:
            if _path(links, source, target, None, 0) is not None:
                base["reason"] = "insufficient_capacity"
            elif _path(links, source, target, None, 0, include_unusable=True) is None:
                base["reason"] = "no_topological_path"
            else:
                # 只检查与本业务连通有关的缺失观测，不能因远处无关边过期误报。
                unknown = {"missing_endpoint_observation", "stale_endpoint_observation", "future_snapshot_time"}
                diagnostic = {lid: row | {"usable": row["usable"] or row["reason"] in unknown,
                                          "delay_ns": row.get("delay_ns", 0)} for lid, row in links.items()}
                base["reason"] = ("stale_or_missing_observation" if _path(diagnostic, source, target, None, 0)
                                  is not None else "no_available_path")
            decisions.append(base)
            continue
        nodes, path_links = found
        estimates, violations = _quality(service, path_links, links,
                                         payload_bytes + epoch_overhead_bytes + 28,
                                         qos_profile=qos_profile)
        if qos_profile == 'official' and violations:
            # 保留候选和超限原因便于解释结果；accepted=False 不触发远端安装。
            base.update(reason='candidate_qos_violation', node_path=nodes,
                        link_path=path_links, quality_estimates=estimates,
                        violations=violations)
            decisions.append(base)
            continue
        for lid in path_links:
            remaining[lid] -= demand  # 反向业务也扣同一lid的剩余量，不建立第二份预算。
        base.update(accepted=True, reason="accepted_with_qos_violations" if violations else "accepted",
                    node_path=nodes, link_path=path_links, reserved_bps=service.required_bps,
                    wire_bps=demand, quality_estimates=estimates, violations=violations)
        decisions.append(base)
    name = ('priority_then_capacity_feasible_min_delay_official_qos'
            if qos_profile == 'official' else 'priority_then_capacity_feasible_min_delay')
    return {"decided_at_ns": now_ns, "policy": name,
            "decisions": decisions, "initial_wire_budget_bps": budgets,
            "retained_wire_bps": retained, "remaining_wire_bps": remaining,
            "control_headroom_fraction": control_headroom_fraction,
            "payload_bytes": payload_bytes, "epoch_overhead_bytes": epoch_overhead_bytes,
            "overcommitted_retained_links": sorted(lid for lid in links if retained.get(lid, 0) > budgets[lid])}
