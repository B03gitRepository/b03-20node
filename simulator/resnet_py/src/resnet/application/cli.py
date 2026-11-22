"""统一命令入口。scripts 中的入口只转发参数，不另建仿真流程。"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run_main(argv=None):
    """执行一个案例，可选只读采样，成功后生成本次运行的报告。

    场景位置相对于配置文件解析；输出路径相对于当前终端目录解析。
    每次只选择一个 case_id，配置中的其他案例不会自动执行。
    """
    parser = argparse.ArgumentParser(description='执行一个网络仿真实验')
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT/'configs/experiment.json')
    parser.add_argument('--case', default='quick', help='实验配置中的 case_id，每次只选一个')
    parser.add_argument('--output', type=Path, required=True, help='新的结果目录')
    parser.add_argument('--quiet', action='store_true', help='不在终端输出运行进度')
    parser.add_argument('--no-report', action='store_true', help='仅保存结果，不生成 HTML 报告')
    parser.add_argument('--telemetry-interval', '--interval', type=float, default=None,
                        help='额外状态采样的间隔，单位秒；省略时不创建采样器')
    parser.add_argument('--reference', type=Path, help='可选：与已有 summary.json 精确核对')
    args = parser.parse_args(argv)

    from .runner import run_case, write_json
    observer_factory = None
    if args.telemetry_interval is not None:
        from resnet.metrics.telemetry import TelemetryRecorder
        observer_factory = lambda sim: TelemetryRecorder(
            sim, args.output/'telemetry.jsonl', args.telemetry_interval)
    result = run_case(args.config, args.case, args.output, progress=not args.quiet,
                      observer_factory=observer_factory)
    if args.telemetry_interval is not None:
        write_json(args.output/'telemetry-metadata.json', {
            'interval_s': args.telemetry_interval,
            'samples': 'simulation timestamps after same-time network events; final endpoint included',
            'queue': 'instantaneous samples; peaks between samples require cumulative queue_peak_packets',
            'interval_counters': 'difference of cumulative counts on (previous_time,current_time]',
            'endpoint_zero': 't=0 events are stored separately and included in the first plotted interval',
        })
    if args.reference:
        reference = json.loads(args.reference.read_text(encoding='utf-8-sig'))
        proof = {'reference': str(args.reference.resolve()), 'summary_exact_match': result == reference}
        proof['different_top_level_keys'] = sorted(
            key for key in set(result) | set(reference) if result.get(key) != reference.get(key))
        write_json(args.output/'reference-check.json', proof)
        if not proof['summary_exact_match']:
            raise ValueError('运行汇总与参考文件不同，详见 reference-check.json')
    print(f"运行结果：{args.output.resolve() / 'summary.json'}", flush=True)
    if not args.no_report:
        from resnet.reporting import generate_report
        print(f'运行报告：{generate_report(args.output)}', flush=True)
    return 0 if result['checks_passed'] else 1


def validate_main(argv=None):
    """只校验场景和编译时间批次；不启动发包或控制器。"""
    parser = argparse.ArgumentParser(description='校验场景文件、引用和动态时间批次')
    parser.add_argument('--scenario', type=Path, required=True)
    args = parser.parse_args(argv)
    from resnet.io import load_scenario
    print(json.dumps(load_scenario(args.scenario).summary(), ensure_ascii=False, indent=2))
    return 0


# 这里只登记命令和目标函数；延迟导入使基础仿真无需安装绘图或训练依赖。
COMMANDS = {
    'run-scenarios': ('resnet.application.suite', 'main', '运行三套场景的两种策略'),
    'report-scenarios': ('resnet.reporting.scenario_comparison', 'main', '生成三场景实际运行报告'),
    'run': ('resnet.application.cli', 'run_main', '运行网络并生成报告'),
    'validate': ('resnet.application.cli', 'validate_main', '校验场景，不执行仿真'),
    'report': ('resnet.reporting.commands', 'report_main', '为已有结果生成报告'),
    'compare': ('resnet.reporting.comparison', 'main', '比较两轮运行结果'),
    'plot': ('resnet.reporting.plots', 'main', '绘制运行结果'),
    'plot-comparison': ('resnet.reporting.plots', 'timeline_main', '绘制对照时间图'),
    'audit': ('resnet.reporting.audit', 'main', '核对事件、来源和计数'),
    'reference': ('resnet.verification.ns3_reference', 'main', '核对 ns-3 保存参考记录'),
    'benchmark': ('resnet.verification.benchmark', 'main', '测量当前机器运行耗时与内存'),
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        module, function, _ = COMMANDS[args[0]]
        return getattr(importlib.import_module(module), function)(args[1:])
    parser = argparse.ArgumentParser(description='资源受限动态网络仿真系统',
        epilog='命令说明：\n' + '\n'.join(f'  {name:16} {row[2]}' for name, row in COMMANDS.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=COMMANDS, help='具体参数见 <command> --help')
    parser.parse_args(args)
    return 0
