"""比较两轮运行的实际记录，不读取当前场景目录，也不预设事件时刻。

输入：固定路由与规则管控的结果目录。输出：analysis.json。
复用单次报告的数据校验和指标公式；缺少控制明细日志不妨碍结果比较。
新增评估指标时先在 data.py 统一定义，再由本模块组织两轮结果。
"""
import argparse
import json
from pathlib import Path

from .data import collect_report


def compare_runs(fixed_dir, control_dir, output_dir):
    """返回比较结果并保存；只有相同场景与运行窗口才可直接对照。

    analysis.json 包含指标、时间曲线、执行事件和逐对象统计。
    数值均来自已完成的运行；绘图入口可单独读取该文件，无需重跑仿真。
    """
    runs = {name: collect_report(directory) for name, directory in
            (('fixed', fixed_dir), ('control', control_dir))}
    fixed, control = runs['fixed'], runs['control']
    for name, report in runs.items():
        if report['config']['mode'] != name:
            raise ValueError(f'{name} 输入目录的实际策略模式不符')
    for key in ('scenario_id', 'source_hashes', 'stop_s', 'events', 'batches'):
        if fixed[key] != control[key]:
            raise ValueError(f'两轮运行不能直接对照：{key} 不一致')
    if not fixed['source_hashes']:
        raise ValueError('结果缺少输入文件摘要，无法确认两轮使用同一场景')
    for key in ('services', 'links'):
        if {item['id'] for item in fixed[key]} != {item['id'] for item in control[key]}:
            raise ValueError(f'两轮运行不能直接对照：{key} 对象编号不一致')
    for key in ('start_s', 'end_s', 'drain_s', 'service_ids', 'seed',
                'queue_packets', 'queue_bytes', 'payload_bytes', 'rate_scale'):
        if fixed['config'].get(key) != control['config'].get(key):
            raise ValueError(f'两轮运行不能直接对照：配置 {key} 不一致')
    # 用实际批次作为区间边界。环境变化和业务活动切换均来自本轮日志。
    boundaries = sorted({0.0, fixed['stop_s'], fixed['config']['start_s'],
                         fixed['config']['end_s']} |
                        {batch['time_s'] for batch in fixed['batches']})
    intervals = [dict(start_s=a, end_s=b,
                      event_ids=[e['id'] for e in fixed['events'] if e['time_s'] == a])
                 for a, b in zip(boundaries, boundaries[1:]) if b > a]
    result = dict(schema='resnet-comparison-v1', scenario_id=fixed['scenario_id'],
                  stop_s=fixed['stop_s'], events=fixed['events'], intervals=intervals,
                  runs=runs, notes=[
                      '业务指标复用单次报告的计算口径；时延按实际收包数加权。',
                      '事件取自实际执行日志；队列为定期采样，不代表采样间隔内的全部变化。',
                      '固定路由和规则管控具有不同动作权限及控制开销，应结合逐业务结果解释。'])
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('分析目录已有内容，请选择新的目录')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'analysis.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixed', type=Path, required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    compare_runs(args.fixed, args.control, args.output)
    print(f'对照数据：{(args.output / "analysis.json").resolve()}')
    return 0
