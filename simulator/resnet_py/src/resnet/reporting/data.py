"""报告的数据整理层：读本轮文件、核对计数、计算指标和时间曲线。

输入：一次运行目录。依赖：Python 标准库。输出：普通 dict / list，供渲染层使用。
指标分母在这里统一定义；没有样本时返回 None，页面显示“—”，避免误报为零。
"""
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


EVENT_NAMES = {
    'link_param_change': '链路参数变化', 'link_state': '链路通断',
    'node_offline': '节点下线', 'node_online': '节点上线',
    'node_move': '节点移动', 'service_adjust': '业务调整',
}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def ratio(a, b):
    return 100 * a / b if b else None


def metrics(row, demand_bits=None, pending=0):
    """按收包数加权计算时延；需求完成度保留停源期间尚未满足的需求。"""
    generated = row['generated_packets']
    received = row['delivered_packets']
    return {
        'generated': generated, 'delivered': received, 'dropped': row['dropped_packets'],
        'pending': pending, 'delivery_pct': ratio(received, generated),
        'loss_pct': ratio(row['dropped_packets'], generated),
        'mean_delay_ms': row['delay_sum_ns'] / received / 1e6 if received else None,
        'max_delay_ms': row.get('delay_max_ns', 0) / 1e6 if received else None,
        'demand_bits': demand_bits,
        'demand_delivery_pct': ratio(row['delivered_payload_bytes'] * 8, demand_bits),
        'demand_timely_pct': ratio(row.get('deadline_success_payload_bytes', 0) * 8, demand_bits),
        'received_timely_pct': ratio(row.get('deadline_success_packets', 0), received),
        'drop_reasons': row.get('drop_reasons', {}),
    }


def chart_series(rows, stop_s):
    """将事件计数按时间窗累加；最后一窗使用实际时长，终点事件纳入最后一窗。

    timeseries.json 每行是 [time_s, time_s+1) 的计数，不是累计值。
    不对每秒平均时延再次求平均，而是先累加总时延和收包数，再相除。
    """
    if stop_s <= 0:
        return {'window_s': None, 'points': []}
    width = next((s for s in (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800, 3600)
                  if stop_s / s <= 300), math.ceil(stop_s / 300))
    count = math.ceil(stop_s / width)
    keys = ('delivered_payload_bytes', 'delivered_packets', 'dropped_packets', 'delay_sum_ns')
    bins = [{k: 0 for k in keys} for _ in range(count)]
    for row in rows:
        t = row['time_s']
        if t < 0 or t > stop_s:
            raise ValueError('timeseries.json 中存在超出运行范围的时间')
        # stop_s 恰好位于窗边界时，不能另造一个时长为零的窗口。
        index = min(int(t // width), count - 1)
        for key in keys:
            bins[index][key] += row[key]
    points = []
    for i, row in enumerate(bins):
        start, end = i * width, min((i + 1) * width, stop_s)
        received = row['delivered_packets']
        points.append(dict(time_s=(start + end) / 2, start_s=start, end_s=end,
            goodput_mbps=row['delivered_payload_bytes'] * 8 / (end - start) / 1e6,
            delay_ms=row['delay_sum_ns'] / received / 1e6 if received else None,
            dropped=row['dropped_packets'], delivered=received, sampled_queue_max=None))
    return {'window_s': width, 'points': points}


def read_telemetry(path, summary, series):
    """逐行读取大文件；只保存各窗队列采样峰值，不把整份逐秒日志载入内存。"""
    digest = hashlib.sha256()
    previous = None
    first = None
    count = 0
    step_min = step_max = None
    peak = 0
    prev_totals = None
    with path.open('rb') as handle:
        for line in handle:
            digest.update(line)
            row = json.loads(line)
            t = row['time_ns']
            if t < 0 or t > summary['simulation_time_ns'] or (previous is not None and t <= previous):
                raise ValueError('telemetry.jsonl 的时间戳超出范围或未严格递增')
            if previous is not None:
                step = (t - previous) / 1e9
                step_min = step if step_min is None else min(step_min, step)
                step_max = step if step_max is None else max(step_max, step)
            for kind in ('data', 'control'):
                for key, value in row['totals'][kind].items():
                    if key == 'drop_reasons':
                        continue
                    if prev_totals is not None and value < prev_totals[kind][key]:
                        raise ValueError('telemetry.jsonl 中累计计数出现回退')
            first = t if first is None else first
            previous = t
            prev_totals = row['totals']
            count += 1
            queued = sum(link['queue_packets'] for link in row['links'].values())
            peak = max(peak, queued)
            points = series['points']
            if points:
                index = min(int(t / 1e9 // series['window_s']), len(points) - 1)
                old = points[index]['sampled_queue_max']
                points[index]['sampled_queue_max'] = queued if old is None else max(old, queued)
    if first != 0 or previous != summary['simulation_time_ns']:
        raise ValueError('逐秒采样未覆盖完整运行起止时刻；请等待运行结束后再生成报告')
    for kind in ('data', 'control'):
        for key, value in prev_totals[kind].items():
            if value != summary['totals'][kind][key]:
                raise ValueError(f'采样终值与汇总不一致：{kind}.{key}')
    return dict(samples=count, first_s=first / 1e9, last_s=previous / 1e9,
                min_interval_s=step_min, max_interval_s=step_max, sampled_queue_peak=peak,
                sha256=digest.hexdigest())


def read_events(path, stop_ns):
    """从实际执行日志读取事件，而不回读可能已被用户修改的原始剧本。"""
    events, batches = [], []
    if not path.exists():
        return events, batches
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            row = json.loads(line)
            if not 0 <= row['time_ns'] <= stop_ns:
                raise ValueError('批次日志超出运行时段')
            ops = row['operations']
            state = row.get('state', {})
            batches.append(dict(time_s=row['time_ns'] / 1e9, sequence=row['sequence'],
                profile=state.get('profile'),
                active_services=len(state.get('active', state.get('active_service_ids', []))),
                online_nodes=sum(state.get('node_online', {}).values()),
                available_links=sum(state.get('link_effective_up', {}).values())))
            for op in ops:
                if op['kind'] == 'official_event':
                    payload = op['payload']
                    if abs(payload['time'] * 1e9 - row['time_ns']) > 1:
                        raise ValueError('事件脚本时间与执行批次时间不一致')
                    events.append(dict(time_s=row['time_ns'] / 1e9, id=op.get('event_id', ''),
                        type=payload['eventType'], attrs=payload['attrs']))
    return events, batches


def collect_report(run_dir):
    root = Path(run_dir).resolve()
    # performance.json 在运行器最后写入；缺少它通常说明运行尚未结束。
    for name in ('summary.json', 'performance.json', 'timeseries.json'):
        if not (root / name).is_file():
            raise ValueError(f'尚未找到完整运行结果 {name}，请等待仿真结束后再生成报告。')
    summary = read_json(root / 'summary.json')
    stop_ns = summary['simulation_time_ns']
    stop_s = stop_ns / 1e9
    config = summary['config']
    if stop_ns != round((config['end_s'] + config['drain_s']) * 1e9):
        raise ValueError('汇总的结束时刻未达到配置结束时刻')
    warnings = []
    evidence = {}
    for name in ('summary.json', 'performance.json', 'timeseries.json', 'effective-config.json',
                 'batches.jsonl', 'services.csv', 'telemetry-metadata.json', 'output-metadata.json'):
        path = root / name
        if path.exists():
            evidence[name] = dict(bytes=path.stat().st_size,
                                 sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    performance = read_json(root / 'performance.json')
    effective = read_json(root / 'effective-config.json') if (root / 'effective-config.json').exists() else {}
    if effective and summary.get('source_hashes') != effective.get('source_hashes'):
        raise ValueError('summary.json 与 effective-config.json 的 source_hashes 不一致')
    origin = read_json(root / 'output-metadata.json') if (root / 'output-metadata.json').exists() else {}
    rows = read_json(root / 'timeseries.json')
    series = chart_series(rows, stop_s)
    # 两个独立统计文件相互核对，避免把不完整或混用的文件生成漂亮但失真的报告。
    totals = summary['totals']['data']
    for key in ('generated_packets', 'delivered_packets', 'dropped_packets', 'delivered_payload_bytes', 'delay_sum_ns'):
        if sum(row[key] for row in rows) != totals[key]:
            raise ValueError(f'timeseries.json 与 summary.json 不一致：{key}')
    telemetry = None
    if (root / 'telemetry.jsonl').exists():
        telemetry = read_telemetry(root / 'telemetry.jsonl', summary, series)
        evidence['telemetry.jsonl'] = dict(bytes=(root / 'telemetry.jsonl').stat().st_size,
                                          sha256=telemetry['sha256'])
    else:
        warnings.append('本轮未记录 telemetry.jsonl；吞吐、时延和丢包曲线来自逐秒事件统计，队列时间曲线不可用。')
    events, batches = read_events(root / 'batches.jsonl', stop_ns)
    if (root / 'batches.jsonl').exists():
        if dict(Counter(e['type'] for e in events)) != summary['core_event_counts']:
            raise ValueError('实际事件日志与汇总事件计数不一致')
        if len(batches) != summary['batch_count']:
            raise ValueError('实际事件批次数与汇总不一致')
    else:
        warnings.append('本轮缺少批次日志，无法还原事件明细。')
    metadata = {}
    if (root / 'services.csv').exists():
        with (root / 'services.csv').open(encoding='utf-8-sig', newline='') as handle:
            metadata = {r['service_id']: r for r in csv.DictReader(handle)}
    services = []
    for sid, row in sorted(summary['services'].items()):
        demand = summary['demand_bits_by_service'].get(sid, 0)
        spec = metadata.get(sid, {})
        services.append(dict(id=sid, type=spec.get('service_type', ''),
            source=spec.get('source_node', ''), destination=spec.get('destination_node', ''),
            required_bps=spec.get('required_bps'),
            **metrics(row, demand, row['generated_packets'] - row['delivered_packets'] - row['dropped_packets'])))
    links = []
    for lid, row in sorted(summary['links'].items()):
        links.append(dict(id=lid, endpoints=row['endpoints'], capacity_bps=row['capacity_bps'],
            delay_ms=row['delay_ms'], up=row['effective_up'], queue_peak=row['queue_peak_packets'],
            queue_limit=row['queue_limit_packets'], drops=row['drops'],
            mean_queue_ms=row['queue_wait_sum_ns'] / row['queue_wait_count'] / 1e6 if row['queue_wait_count'] else None,
            max_queue_ms=row['queue_wait_max_ns'] / 1e6,
            busy_pct=ratio(row['busy_ns'], stop_ns)))
    demand_bits = sum(summary['demand_bits_by_service'].values())
    data = metrics(totals, demand_bits, summary['conservation']['data']['pending'])
    control = metrics(summary['totals']['control'], pending=summary['conservation']['control']['pending'])
    assessments = [
        f"业务包生成 {data['generated']:,} 个，送达 {data['delivered']:,} 个，终结丢弃 {data['dropped']:,} 个，结束时仍在途 {data['pending']:,} 个。",
        f"本轮实际执行 {len(events)} 条核心事件、{len(batches)} 个场景批次；有活动需求的业务 {sum(s['demand_bits'] > 0 for s in services)} 项。",
    ]
    if totals['drop_reasons']:
        reason, count = max(totals['drop_reasons'].items(), key=lambda r: r[1])
        assessments.append(f"记录最多的丢弃原因是 {reason}，共 {count:,} 个，占终结丢弃的 {ratio(count, data['dropped']):.2f}%。")
    if config['start_s']:
        warnings.append(f"本轮在场景第 {config['start_s']} 秒启动业务，采用冷启动；起点之前的批次用于建立状态，没有之前遗留的数据包。")
    if config.get('mode') == 'control':
        warnings.append('当前规则计算的仿真耗时设为 0；现实控制器计算累计耗时另行记录，尚未注入网络时间轴。')
    if not summary['checks_passed']:
        warnings.append('仿真内部一致性检查未通过，请先检查计数与在途账本，再解释业务表现。')
    return dict(schema='resnet-run-report-v1', generated_at=datetime.now(timezone.utc).isoformat(),
        run_dir=str(root), case_id=summary.get('case_id', ''), scenario_id=origin.get('scenario', ''),
        dataset_official=origin.get('dataset_official'), config=config, stop_s=stop_s,
        node_count=summary['node_count'], link_count=summary['link_count'],
        summary_checks_passed=summary['checks_passed'], performance=performance,
        data=data, control=control, services=services, links=links, series=series,
        events=events, batches=batches, telemetry=telemetry,
        event_counts=summary['core_event_counts'], control_details=summary.get('control', {}),
        conservation=summary['conservation'], assessments=assessments, warnings=warnings,
        source_hashes=summary.get('source_hashes', {}), config_sha256=effective.get('config_sha256'),
        evidence=evidence)
