"""带内控制参数：默认值、单位与输入校验。

所有 *_ns 字段均为整数纳秒；省略 managed_service_ids 时管理全部已知业务定义。
此处只校验配置，状态报告仍须经网络到达后才能参与决策。
"""
from copy import deepcopy
import math


def control_defaults(options=None, *, service_ids=(), node_ids=()):
    """补齐默认参数并拒绝会造成零间隔死循环或失效校验的设置。

    输入可只写想修改的字段；输出独立字典，不修改调用者的配置。主备链路
    默认关闭，EXT20 配置再明确提供 L00015/L00016，便于复用到其它拓扑。
    """
    result = {
        'controller_id': 'GND001', 'mode': 'closed_loop',
        'qos_profile': 'baseline',
        'managed_service_ids': sorted(service_ids),
        'observation_ttl_ns': 10_000_000_000,
        'command_validity_ns': 5_000_000_000,
        'probe_interval_ns': 5_000_000_000,
        'service_report_interval_ns': 5_000_000_000,
        'decision_interval_ns': 1_000_000_000,
        'tick_ns': 100_000_000,
        'ack_wait_ns': 1_500_000_000,
        'max_retries': 2,
        'query_interval_ns': 2_000_000_000,
        'prepare_lease_ns': 30_000_000_000,
        'old_rule_retention_ns': 2_000_000_000,
        'transaction_timeout_ns': 45_000_000_000,
        'control_headroom_fraction': .05,
        'port_detection_ns': 200_000_000,
        'control_primary_link': None, 'control_backup_link': None,
    }
    # 配置拼错时立即报告，避免用户以为已调参，实际却一直使用默认值。
    if options is not None and not isinstance(options, dict):
        raise TypeError('control 配置必须为字典或 None')
    supplied = {} if options is None else options
    unknown = set(supplied) - set(result)
    if unknown:
        raise ValueError(f'control 配置包含未知字段：{sorted(map(str, unknown))}')
    result.update(deepcopy(supplied))
    if not isinstance(result['qos_profile'], str) or result['qos_profile'] not in ('baseline', 'official'):
        raise ValueError('qos_profile 必须为 baseline 或 official')
    controller_id = result['controller_id']
    if not isinstance(controller_id, str) or not controller_id.strip():
        raise ValueError('controller_id 必须为非空节点 ID 字符串')
    for key in ('control_primary_link', 'control_backup_link'):
        value = result[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f'{key} 必须为非空链路 ID 字符串或 None')
    for key in tuple(result):
        if key.endswith('_ns'):
            value = result[key]
            if type(value) is not int or value <= 0:
                raise ValueError(f'{key} 必须为正整数纳秒')
    if type(result['max_retries']) is not int or result['max_retries'] < 0:
        raise ValueError('max_retries 必须为非负整数')
    fraction = result['control_headroom_fraction']
    if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction < 1:
        raise ValueError('control_headroom_fraction 必须在 [0, 1) 内')
    managed = result['managed_service_ids']
    if (not isinstance(managed, list) or any(not isinstance(sid, str) for sid in managed)
            or len(set(managed)) != len(managed)):
        raise ValueError('managed_service_ids 必须为不重复的业务 ID 列表')
    if set(managed) - set(service_ids):
        raise ValueError('managed_service_ids 包含未知业务')
    if node_ids and result['controller_id'] not in node_ids:
        raise ValueError('controller_id 不在场景节点集合中')
    if result['mode'] != 'closed_loop':
        raise ValueError('此适配器实现 closed_loop；异常命令测试直接通过 send_control 注入')
    if (result['control_primary_link'] is None) != (result['control_backup_link'] is None):
        raise ValueError('管控主备链路必须成对配置')
    return result

