"""报告命令入口；具体计算和渲染由 reporting 包内模块承担。"""
import argparse
from pathlib import Path

from . import generate_report


def report_main(argv=None):
    parser = argparse.ArgumentParser(description='从本轮结果补生成 HTML 报告，不重跑仿真')
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        path = generate_report(args.run)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f'报告未生成：{error}\n')
    print(f'本次运行报告：{path}')
    return 0
