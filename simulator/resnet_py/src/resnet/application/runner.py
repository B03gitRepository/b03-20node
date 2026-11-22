"""配置入口、进度、落盘与赛事兼容输出。

输入 JSON 配置和案例名；输出本轮独立目录。主要调用 Simulation.advance，
不在运行器中修改接收计数。初学者可先看 run_case，再逐层进入网络代码。
"""
from __future__ import annotations

import csv
import json
from itertools import chain
from pathlib import Path
import platform
import time

from .config import load_config
from resnet.formats.clock import ClockMapping
from resnet.formats.link_status import LinkStatus
from resnet.simulation.simulation import Simulation


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def run_case(config_path, case_id, output_dir, *, progress=True, observer_factory=None, decision_policy=None, diagnosis_policy=None):
    """执行完整案例并保存结果；拒绝覆盖非空结果目录。"""
    scenario, config, source, config_hash = load_config(config_path, case_id)
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('输出目录已有内容，请选择新的运行目录')
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    sim = Simulation(scenario, config, output, decision_policy=decision_policy,
                     diagnosis_policy=diagnosis_policy)
    write_json(output/'effective-config.json', dict(scenario_source=str(source),
        source_hashes=scenario.source_hashes, config_sha256=config_hash, case_id=case_id,
        simulation=sim.config, scenario_summary=scenario.summary()))
    observer = None
    try:
        # 可选的只读采样器：记录网络状态，不参与选路、发包或随机数抽样。
        # 工厂接收本轮 Simulation，返回具有 close() 的采样器；普通运行无需传入。
        if observer_factory is not None:
            observer = observer_factory(sim)
        step = round(sim.config['progress_interval_s'] * 1e9)
        # 惰性遍历，不因长场景或较短进度间隔一次性分配庞大时间表。
        boundaries = chain(range(step, sim.stop_ns, step), [sim.stop_ns])
        for endpoint in boundaries:
            sim.advance(endpoint)
            state = dict(time_s=sim.now/1e9, stop_s=sim.stop_ns/1e9,
                wall_elapsed_s=round(time.perf_counter()-started, 3),
                generated_packets=sim.metrics.totals['data']['generated_packets'],
                delivered_packets=sim.metrics.totals['data']['delivered_packets'],
                pending_packets=len(sim.pending))
            write_json(output/'progress.json', state)
            if progress:
                print(f"[{state['time_s']:.0f}/{state['stop_s']:.0f} s] "
                      f"generated={state['generated_packets']} delivered={state['delivered_packets']} "
                      f"wall={state['wall_elapsed_s']:.1f}s", flush=True)
        result = sim.summary()
        result['case_id'] = case_id
        result['source_hashes'] = scenario.source_hashes
        write_json(output/'summary.json', result)
        write_json(output/'timeseries.json', [sim.metrics.timeseries[t] for t in sorted(sim.metrics.timeseries)])
        fields = ['service_id','service_type','source_node','destination_node','required_bps',
                  'demand_bits','generated_packets','delivered_packets','dropped_packets',
                  'delivered_payload_bytes','mean_delay_ms','deadline_success_packets']
        with (output/'services.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for sid, spec in sorted(sim.services.items()):
                stats = result['services'][sid]
                writer.writerow(dict(service_id=sid, service_type=spec.service_type,
                    source_node=spec.src_node_id, destination_node=spec.dst_node_id,
                    required_bps=spec.required_bps, demand_bits=result['demand_bits_by_service'][sid],
                    **{k: stats[k] for k in fields[6:] if k != 'demand_bits'}))
        # 沿用赛事字段结构；项目使用的固定时间起点在独立元数据中记录。
        clock = ClockMapping.from_iso('2026-01-01T00:00:00+00:00', offset_minutes=0)
        records = []
        for spec in scenario.links:
            # 参数与通断均读取实际链路；SceneState 不再保存链路参数副本。
            link = sim.links[spec.link_id]
            row = link.parameters
            up = link.effective_up
            records.append(LinkStatus(spec.link_id, spec.link_type, spec.src_node_id, spec.dst_node_id,
                'up' if up else 'down', int(row['capacity_bps']), row['delay_ms']/1000,
                row['jitter_ms']/1000, row['ber'], row['packet_loss_rate'], sim.now//1000
                ).to_wire(clock, 'pdf_compat'))
        write_json(output/'link-status-pdf.json', records)
        write_json(output/'output-metadata.json', dict(profile='pdf_compat',
            epoch_origin='2026-01-01T00:00:00+00:00', epoch_source='project_reproducible_mapping',
            parameters='configured_link_values_not_measured_packet_qos',
            ber='metadata_only',
            dataset_official=scenario.raw_documents.get('scenario-config.json', {}).get('official_dataset'),
            scenario=scenario.scenario_id,
            scenario_source=scenario.raw_documents.get('scenario-config.json', {}).get('source_scenario')))
        write_json(output/'performance.json', dict(wall_elapsed_s=time.perf_counter()-started,
            decision_wall_time_ns=getattr(sim.control, 'decision_wall_time_ns', 0),
            python=platform.python_version(), platform=platform.platform(),
            performance_is_not_deterministic=True))
        return result
    finally:
        if observer is not None:
            observer.close()
        sim.close()
