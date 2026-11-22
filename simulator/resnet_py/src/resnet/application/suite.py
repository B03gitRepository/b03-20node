"""顺序或并行运行三套场景；每套包含固定路由与规则管控。

输入：新结果目录、并行进程数，可选场景列表。
执行：调用既有 run.py 入口，各次仿真使用独立进程、文件夹和随机数状态。
输出：六个运行目录及 suite-status.json。此脚本不参与网络决策或事件调度。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[3]
from resnet.application.config import load_config

# 文件夹名、实验配置、案例名。六次运行只复用配置，不共享仿真对象。
CASES = {
    'test': [('test_fixed', 'test20.json', 'test_fixed'),
             ('test_control', 'test20.json', 'test_control')],
    'official': [('official_fixed', 'official20.json', 'official_fixed'),
                 ('official_control', 'official20.json', 'official_control')],
    'aligned': [('aligned_fixed', 'experiment.json', 'full_fixed'),
                ('aligned_control', 'experiment.json', 'full_control')],
}


def execute(row, output):
    """启动一轮完整仿真，把终端进度保存到独立日志；返回退出状态。"""
    name, config, case = row
    command = [sys.executable, str(PROJECT / 'scripts/run.py'), 'run',
               '--config', str(PROJECT / 'configs' / config), '--case', case,
               '--telemetry-interval', '1', '--output', str(output / name)]
    started = datetime.now(timezone.utc).isoformat()
    with (output / f'{name}.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT,
            env=os.environ | {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return dict(name=name, config=config, case=case, started_utc=started,
                finished_utc=datetime.now(timezone.utc).isoformat(), returncode=result.returncode,
                status='complete' if result.returncode == 0 else 'failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='新建的三场景结果目录')
    parser.add_argument('--workers', type=int, default=2, help='同时运行的进程数，默认2')
    parser.add_argument('--scenes', nargs='+', choices=list(CASES), default=list(CASES))
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 6:
        parser.error('--workers 应为1至6')
    if len(set(args.scenes)) != len(args.scenes):
        parser.error('--scenes 不应重复')
    rows = [row for name in args.scenes for row in CASES[name]]
    # 先确认所有输入能够解析，避免运行一半才发现另一个场景配置写错。
    for _, config, case in rows:
        load_config(PROJECT / 'configs' / config, case)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('结果目录已有内容，请换一个目录名')
    output.mkdir(parents=True, exist_ok=True)
    status = {name: dict(name=name, config=cfg, case=case, status='scheduled')
              for name, cfg, case in rows}

    def save():
        temporary = output / 'suite-status.tmp'
        temporary.write_text(json.dumps(dict(workers=args.workers, runs=status),
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(output / 'suite-status.json')

    save()
    print(f'开始 {len(rows)} 次运行，结果位置：{output}', flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(execute, row, output): row[0] for row in rows}
        pending = set(futures)
        while pending:
            # 子进程在独立日志中记录细节；主终端每30秒显示一次总体进度。
            # 读取 progress.json 不改变任何仿真状态，也不使用现实时间安排网络事件。
            done, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
            for future in done:
                name = futures[future]
                try:
                    status[name] = future.result()
                except Exception as error:
                    status[name] = dict(name=name, status='failed', error=str(error), returncode=1)
                print(f'{name}: {status[name]["status"]}', flush=True)
            for future in sorted(pending, key=lambda item: futures[item]):
                name = futures[future]
                if not future.running():
                    continue
                status[name]['status'] = 'running'
                try:
                    progress = json.loads((output / name / 'progress.json').read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    # 初次记录尚未写入或正处于写入瞬间，等待下一次刷新即可。
                    continue
                status[name]['last_progress'] = progress
                print(f"{name}: {progress['time_s']:.0f}/{progress['stop_s']:.0f} 仿真秒", flush=True)
            save()
    return 0 if all(row.get('returncode') == 0 for row in status.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
