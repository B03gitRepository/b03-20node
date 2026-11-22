"""运维分析入口：根据已经收到的报告，整理待处理业务与异常说明。

默认规则只整理现有事实，不预测未来、不改变观测值，也不修改节点。
后续运维智能体可以替换 diagnose，返回相同的 Diagnosis 结构。
资源管控智能体的替换位置独立保留在 policy.decide。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnosis:
    """active_service_ids 交给资源策略处理；issues 是供解释与调试的诊断说明。

    自定义诊断可以暂缓某项业务：不把该业务放入 active_service_ids。
    若该业务此前已启用，控制主循环会沿现有停源、确认、清理流程处理。
    返回值每轮更新；正在部署时仍会重新检查原始报告的新鲜度与需求版本。
    """
    active_service_ids: tuple[str, ...]
    issues: tuple[str, ...] = ()


def is_active_demand(row):
    """只有已注册、处于活动期且报告仍新鲜的业务可以进入资源决策。"""
    return bool(row and row['fresh'] and row['registered'] and row['active'])


def diagnose(snapshot, demand_snapshot, *, now_ns):
    """输入为本轮链路/业务观测快照；输出业务候选与当前异常说明。

    now_ns 是本轮仿真时刻。观测的新鲜度由缓存按该时刻计算，诊断沿用其结果。
    链路是否可用不直接决定业务是否停止：资源策略还需检查是否存在其他路径。
    """
    active = tuple(sorted(service_id for service_id, row in demand_snapshot['services'].items()
                          if is_active_demand(row)))
    issues = tuple(f"{link_id}: {row['reason']}"
                   for link_id, row in sorted(snapshot['links'].items()) if not row['usable'])
    return Diagnosis(active, issues)


def validate_diagnosis(result, demand_snapshot):
    """诊断可以筛选收到的需求，但不能凭空添加未知、过期或已停止的需求。"""
    if not isinstance(result, Diagnosis):
        raise TypeError('运维诊断必须返回 Diagnosis')
    if (not isinstance(result.active_service_ids, tuple)
            or any(not isinstance(value, str) for value in result.active_service_ids)
            or len(set(result.active_service_ids)) != len(result.active_service_ids)):
        raise ValueError('active_service_ids 必须是不重复的业务编号元组')
    if not isinstance(result.issues, tuple) or any(not isinstance(value, str) for value in result.issues):
        raise ValueError('issues 必须是说明文字组成的元组')
    available = {service_id for service_id, row in demand_snapshot['services'].items()
                 if is_active_demand(row)}
    if not set(result.active_service_ids) <= available:
        raise ValueError('诊断引用了未收到、已过期或已停止的业务需求')
