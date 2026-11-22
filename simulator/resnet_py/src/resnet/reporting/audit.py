"""独立核对输入来源、事件执行、分组守恒与队列限制。

输入：结果目录和可选的场景目录覆盖。默认读取本轮 effective-config.json。
输出：audit.json 与通过状态。不调用仿真器、状态更新器或策略函数。
"""
import argparse
import hashlib
import json
from pathlib import Path

from .data import read_json


def audit(run_dir, scenario_dir=None):
    """先核对源文件摘要，再逐项对照，避免把修改后的剧本当成本轮输入。

    搬到另一台电脑后，可以显式传入 scenario_dir；文件内容仍必须与运行摘要一致。
    场景未提供 expected_final 时不进行该组可选断言，其余审计正常执行。
    """
    root = Path(run_dir).resolve()
    summary = read_json(root / 'summary.json')
    effective = read_json(root / 'effective-config.json')
    source = Path(scenario_dir) if scenario_dir is not None else Path(effective['scenario_source'])
    if not source.is_absolute():
        source = (root / source) if scenario_dir is None else source
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f'场景目录不存在：{source}；可用 --scenario 指定迁移后的目录')
    hashes = summary.get('source_hashes', {})
    if 'scenario-config.json' not in hashes or hashes != effective.get('source_hashes'):
        raise ValueError('本轮输入摘要缺失或与有效配置不一致')
    for name, expected in hashes.items():
        path = (source / name).resolve()
        if not path.is_relative_to(source):
            raise ValueError(f'输入摘要路径超出场景目录：{name}')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'输入文件摘要不匹配：{name}；请使用运行时的场景副本')
    scenario = read_json(source / 'scenario-config.json')
    script_name = scenario['files']['events']
    script_path = (source / script_name).resolve()
    if not script_path.is_relative_to(source) or script_path.relative_to(source).as_posix() not in hashes:
        raise ValueError('事件脚本不在本轮已核对的输入摘要中')
    script = read_json(script_path)
    raw_events = script.get('events', script.get('script', [])) if isinstance(script, dict) else script
    stop_us = round(summary['config']['end_s'] * 1e6)
    observed = []
    with (root / 'batches.jsonl').open(encoding='utf-8') as handle:
        for line in handle:
            row = json.loads(line)
            observed.extend((row['time_ns'] // 1000, op['event_id'], op['payload'])
                            for op in row['operations'] if op['kind'] == 'official_event')
    expected = [(round(event['time'] * 1e6), f'E{index:02d}', event)
                for index, event in enumerate(raw_events, 1) if round(event['time'] * 1e6) <= stop_us]
    checks = dict(input_hashes_match=True,
                  event_count_matches_source=len(observed) == len(expected),
                  event_payload_and_time_match_source=observed == expected)
    for kind in ('data', 'control'):
        row = summary['totals'][kind]
        pending = summary['conservation'][kind]['pending']
        checks[f'{kind}_conservation'] = row['generated_packets'] == row['delivered_packets'] + row['dropped_packets'] + pending
    checks['link_pending_matches_endpoint_ledger'] = (
        sum(x['pending_count'] for x in summary['links'].values()) ==
        sum(x['pending'] for x in summary['conservation'].values()))
    for lid, link in summary['links'].items():
        checks[f'{lid}_conservation'] = link['attempts'] == link['rx'] + link['drops'] + link['pending_count']
        checks[f'{lid}_queue_limit'] = (link['queue_peak_packets'] <= link['queue_limit_packets'] and
                                       link['queue_peak_bytes'] <= link['queue_limit_bytes'])
        checks[f'{lid}_busy_time_bound'] = 0 <= link['busy_ns'] <= summary['simulation_time_ns']
    expected_final = scenario.get('expected_final')
    skipped = []
    if summary['config']['end_s'] == scenario['duration_s'] and expected_final:
        final = summary['final_state']
        actual = dict(online_nodes=sum(final['node_online'].values()),
                      available_links=sum(final['link_effective_up'].values()),
                      registered_services=len(final['registered_service_ids']),
                      active_services=len(final['active_service_ids']))
        for key, value in expected_final.items():
            if key in actual:
                checks[f'final_{key}'] = actual[key] == value
            else:
                skipped.append(f'未定义审计规则的终态字段：{key}')
    else:
        skipped.append('未提供终态断言，或本轮未运行至场景终点')
    result = dict(checks=checks, checks_passed=all(checks.values()), skipped=skipped,
                  scenario_source=str(source), observed_core_events=len(observed),
                  expected_core_events=len(expected))
    (root / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--scenario', type=Path, help='迁移后的场景位置；摘要仍须与本轮输入一致')
    args = parser.parse_args(argv)
    result = audit(args.run_dir, args.scenario)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['checks_passed'] else 1
