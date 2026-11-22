"""读取三套场景的六次完整运行，生成可离线查看的对照报告。

输入：run_scenarios.py 产生的结果根目录。输出：HTML、三张时间图及 CSV/JSON。
本文件只读实际运行结果，不启动仿真，也不从当前剧本推测已经执行过的事件。
"""
from __future__ import annotations

import argparse
import base64
import csv
from collections import Counter
from html import escape
import hashlib
import json
import math
from pathlib import Path

from resnet.reporting.data import collect_report, read_json, read_telemetry
from resnet.reporting.html import DROP_NAMES

SCENARIOS = (
    ("test", "自建测试场景", "EXT20-02", "01_自建测试场景.png"),
    ("official", "官方基准场景", "sce20261010-conf02/sce01/script-09", "02_官方基准场景.png"),
    ("aligned", "自建规则对齐场景", "EXT20-03", "03_自建规则对齐场景.png"),
)
WINDOW_S = 20


def fixed_window_series(rows, stop_s, window_s=WINDOW_S):
    """把逐秒计数归入固定20秒窗口；时延按收到的包数加权。

    rows 是每秒增量，不是累计计数。最后一个不足20秒的窗口用实际长度计算。
    没有收包时将时延留空，图上断开，避免把没有通信误画为零时延。
    """
    count = math.ceil(stop_s / window_s)
    keys = ("delivered_payload_bytes", "delivered_packets", "dropped_packets", "delay_sum_ns")
    bins = [{key: 0 for key in keys} for _ in range(count)]
    for row in rows:
        t = row["time_s"]
        if t < 0 or t > stop_s:
            raise ValueError("时间统计超出本轮仿真范围")
        slot = min(int(t // window_s), count - 1)
        for key in keys:
            bins[slot][key] += row[key]
    points = []
    for i, row in enumerate(bins):
        start, end = i * window_s, min((i + 1) * window_s, stop_s)
        received = row["delivered_packets"]
        points.append({
            "time_s": (start + end) / 2, "start_s": start, "end_s": end,
            "goodput_mbps": row["delivered_payload_bytes"] * 8 / (end - start) / 1e6,
            "delay_ms": row["delay_sum_ns"] / received / 1e6 if received else None,
            "dropped": row["dropped_packets"], "delivered": received,
            "sampled_queue_max": None,
        })
    return {"window_s": window_s, "points": points}


def read_run(path):
    """复用单次报告的完整性核对，再统一本报告的统计窗口。"""
    report = collect_report(path)
    if not report["summary_checks_passed"]:
        raise ValueError(f"{path.name} 的计数不一致，不能生成对照报告")
    if report["telemetry"] is None:
        raise ValueError(f"{path.name} 缺少队列采样，请运行时加 --telemetry-interval 1")
    # 保留首次扰动前的实际逐秒业务计数和管控收发计数，供两套完整需求场景对照。
    rows = read_json(path / "timeseries.json")
    report["before_first_official_event"] = [row for row in rows if 0 <= row["time_s"] < 180]
    if report["series"]["window_s"] != WINDOW_S:
        summary = read_json(path / "summary.json")
        report["series"] = fixed_window_series(rows, report["stop_s"])
        read_telemetry(path / "telemetry.jsonl", summary, report["series"])
    return report


def comparable(reports):
    """防止把旧baseline、不同实验时长或不同队列条件混进本轮比较。"""
    shared = ("seed", "payload_bytes", "rate_scale", "queue_packets", "queue_bytes",
              "start_s", "end_s", "drain_s")
    reference = reports["test_fixed"]["config"]
    control_reference = reports["test_control"]["config"]["control"]
    hashes = reports["official_fixed"]["source_hashes"]
    for prefix, _, scenario_id, _ in SCENARIOS:
        pair = [reports[f"{prefix}_{mode}"] for mode in ("fixed", "control")]
        if any(row["scenario_id"] != scenario_id for row in pair):
            raise ValueError(f"{prefix} 的场景编号不匹配")
        if pair[0]["events"] != pair[1]["events"] or pair[0]["source_hashes"] != pair[1]["source_hashes"]:
            raise ValueError(f"{prefix} 的两种策略使用了不同的输入或事件")
        if pair[0]["data"]["demand_bits"] != pair[1]["data"]["demand_bits"]:
            raise ValueError(f"{prefix} 的两种策略需求统计分母不同")
        for mode, row in zip(("fixed", "control"), pair):
            for name in ("net-topo.json", "bg-service.json"):
                if row["source_hashes"].get(name) != hashes.get(name):
                    raise ValueError(f"{prefix}_{mode} 的初始网络或业务定义与官方输入不一致")
            if row["config"]["mode"] != mode:
                raise ValueError(f"{prefix}_{mode} 的实际模式不匹配")
            if any(row["config"][key] != reference[key] for key in shared):
                raise ValueError("六次运行的共同实验参数不一致")
            if row["telemetry"]["min_interval_s"] != 1 or row["telemetry"]["max_interval_s"] != 1:
                raise ValueError("本报告要求完整的1秒状态采样")
        if pair[1]["config"]["control"].get("qos_profile") != "official":
            raise ValueError("规则管控必须使用统一的 official 质量判定模式")
        if pair[1]["config"]["control"] != control_reference:
            raise ValueError("三套规则管控使用了不同的控制参数")
    for mode in ("fixed", "control"):
        if reports[f"official_{mode}"]["before_first_official_event"] != reports[f"aligned_{mode}"]["before_first_official_event"]:
            raise ValueError("官方基准场景与自建规则对齐场景在首次扰动前的逐秒计数不一致")


def event_description(event):
    """将日志中的字段展开为读者能直接查看的事件说明，保留五项链路指标。"""
    attrs, kind = event["attrs"], event["type"]
    if kind == "link_param_change":
        names = (("bandwidthBps", "带宽", "bps"), ("bandwitdhBps", "带宽", "bps"),
                 ("delayMs", "时延", "ms"), ("jitterMs", "抖动", "ms"),
                 ("ber", "BER", ""), ("packetLossRate", "丢包率", ""))
        values = [f"{label} {attrs[key]:g} {unit}".strip() for key, label, unit in names if key in attrs]
        return attrs["linkId"], "；".join(values)
    if kind == "service_adjust":
        requirement = attrs["requirement"]
        return attrs["serviceId"], (
            f"新增 {attrs['serviceType']}，{attrs['srcNodeId']} → {attrs['dstNodeId']}；"
            f"需求 {requirement['bandwidthBps']:g} bps，优先级 {attrs['priority']}；"
            f"时延≤{requirement['delayMs']:g} ms，抖动≤{requirement['jitterMs']:g} ms，"
            f"BER≤{requirement['ber']:g}，丢包率≤{requirement['packetLossRate']:g}")
    if kind == "node_move":
        return attrs["nodeId"], (f"位置 ({attrs['fromX']:g}, {attrs['fromY']:g}) → "
                                 f"({attrs['toX']:g}, {attrs['toY']:g})")
    if kind in ("node_online", "node_offline"):
        return attrs["nodeId"], "上线" if kind == "node_online" else "离线"
    if kind == "link_state":
        return attrs["linkId"], "链路 up" if attrs["state"] == "up" else "链路 down"
    return "—", json.dumps(attrs, ensure_ascii=False)


def draw_scenario(fixed, control, title, target):
    """同一横轴对齐事件与四项运行指标；每张图只比较一种场景的两种策略。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError as error:
        raise RuntimeError("绘图需要 Matplotlib：python -m pip install matplotlib==3.10.8") from error
    available = {font.name for font in font_manager.fontManager.ttflist}
    fonts = [f for f in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS") if f in available]
    plt.rcParams.update({"font.family": fonts + ["DejaVu Sans"], "axes.unicode_minus": False,
                         "font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(5, 1, figsize=(15, 13), sharex=True,
                             gridspec_kw={"height_ratios": [1.15, 1, 1, 1, 1]})
    fig.suptitle(f"{title}  事件与网络运行", x=.14, y=.98, ha="left", fontsize=21, color="#19364b", weight="bold")
    fig.text(.14, .946, f"{fixed['scenario_id']}  |  {fixed['config']['end_s']:g} s 业务期 + "
             f"{fixed['config']['drain_s']:g} s 收尾  |  同一场景、种子和网络规则", fontsize=11, color="#526777")
    lane = {"link_param_change": 3, "service_adjust": 2, "node_move": 1,
            "node_offline": 1, "node_online": 1, "link_state": 0}
    colours = {3: "#327da5", 2: "#8260a4", 1: "#b4822f", 0: "#bc5257"}
    counts = {level: sum(lane.get(e["type"]) == level for e in fixed["events"]) for level in range(4)}
    for level in range(4):
        axes[0].axhline(level, color="#d1dae2", lw=.8)
    for i, event in enumerate(fixed["events"]):
        level = lane[event["type"]]
        y = level + (.09 if i % 2 else -.09)
        axes[0].scatter(event["time_s"], y, s=30, color=colours[level], zorder=3)
        for ax in axes:
            ax.axvline(event["time_s"], color="#dae2e8", lw=.65, zorder=0)
    axes[0].set_yticks([3, 2, 1, 0], [f"链路参数 · {counts[3]}", f"新增业务 · {counts[2]}",
                                     f"移动及上下线 · {counts[1]}", f"链路通断 · {counts[0]}"])
    axes[0].set_ylim(-.5, 3.5)
    keys = ("goodput_mbps", "delay_ms", "dropped", "sampled_queue_max")
    labels = ("业务交付速率\nMbps", "已收包平均时延\nms", "终结丢包\n千包 / 20 s", "全网等待队列采样峰值\n包")
    for report, label, colour in ((fixed, "固定路由", "#327da5"), (control, "规则管控", "#d78534")):
        points = report["series"]["points"]
        x = [p["time_s"] for p in points]
        for ax, key in zip(axes[1:], keys):
            y = [p[key] / 1000 if key == "dropped" else p[key] for p in points]
            ax.plot(x, y, label=label, color=colour, lw=1.6)
    # 两条曲线全部画完再设置纵轴下限，避免第二条曲线的高峰被裁掉。
    for ax, label in zip(axes[1:], labels):
        ax.set_ylabel(label)
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color="#e8ebee", lw=.7)
    axes[1].legend(loc="best", ncol=2)
    for ax in axes:
        ax.axvspan(fixed["config"]["end_s"], fixed["stop_s"], color="#edf0f4", zorder=0)
        ax.axvline(fixed["config"]["end_s"], color="#7f8790", ls="--", lw=.9)
    axes[-1].set_xlim(0, fixed["stop_s"])
    axes[-1].set_xlabel("仿真时间 / s")
    ticks = list(range(0, int(fixed["stop_s"]) + 1, 600))
    if fixed["stop_s"] not in ticks:
        ticks.append(fixed["stop_s"])
    axes[-1].set_xticks(ticks)
    fig.text(.14, .052, "曲线按20秒窗口汇总；队列为1秒采样值在窗口内的最大值，采样间瞬时峰值可能更高。", fontsize=10, color="#526777")
    fig.text(.14, .031, "时延只统计已送达业务包；灰色为停止新业务发包后的收尾期。事件点的上下错开仅用于避免重叠。", fontsize=10, color="#526777")
    fig.subplots_adjust(left=.14, right=.975, top=.91, bottom=.102, hspace=.26)
    fig.savefig(target, dpi=170, facecolor="white")
    plt.close(fig)


def number(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def table(headers, rows):
    return "<div class='table-wrap'><table><thead><tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers) + \
        "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows) + \
        "</tbody></table></div>"


def activity_state_rows(report):
    """从已执行批次提取活动及连通状态，只显示变化行和业务期结束行。

    活动业务数表示场景中正在提出需求的业务数量，不表示已获批准发包的数量。
    去重只用于报告排版，不修改原始批次，也不补造缺失的时刻或状态。
    """
    keys = ("profile", "active_services", "online_nodes", "available_links")
    previous = None
    rows = []
    for batch in report["batches"]:
        state = tuple(batch.get(key) for key in keys)
        at_business_end = math.isclose(batch["time_s"], report["config"]["end_s"], abs_tol=1e-9)
        if state != previous or at_business_end:
            rows.append([number(batch["time_s"], 0),
                         batch.get("profile") if batch.get("profile") is not None else "—",
                         *(number(batch.get(key), 0) for key in keys[1:])])
        previous = state
    return rows


def read_control_wait(path, report):
    """提取最后一次决策和末态未完成事务，不读取逐包传输日志。

    summary 的事务对象说明结束时仍在等待什么；controller.jsonl 说明最后何时
    进行过路径决策。二者分别保留，避免仅凭低交付率推断控制器停止工作。
    """
    details = report["control_details"]
    transaction = details.get("transaction") or {}
    pending = details.get("pending") or {}
    command = pending.get("command") or {}
    uncertain = transaction.get("uncertain_command") or {}
    plan_id = transaction.get("plan_id")
    target = uncertain.get("target_node_id") or command.get("target_node_id")
    last_decision_s = None
    decision_count = 0
    timeout_counts = Counter()
    transaction_started_s = None
    log_path = path / "controller.jsonl"
    digest = hashlib.sha256()
    if log_path.exists():
        # 逐行扫描，只保存少量事实；不把数百MB的控制日志装入内存。
        with log_path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                row = json.loads(line)
                time_s = row.get("time_ns", 0) / 1e9
                if not 0 <= time_s <= report["stop_s"]:
                    raise ValueError(f"{path.name} 的控制日志时间超出本轮运行范围")
                if row.get("event") == "decision":
                    last_decision_s = time_s
                    decision_count += 1
                if plan_id is not None and row.get("plan_id") == plan_id:
                    if row.get("event") == "command_timeout":
                        timeout_counts[row.get("operation", "unknown")] += 1
                    if row.get("event") == "transaction" and row.get("phase") in ("begin", "stop_begin"):
                        transaction_started_s = time_s
    node_changes = [event for event in report["events"]
                    if event["type"] in ("node_offline", "node_online")
                    and event["attrs"].get("nodeId") == target]
    last_node_change = node_changes[-1] if node_changes else None
    return {
        "case_id": report["case_id"], "scenario_id": report["scenario_id"],
        "stop_s": report["stop_s"], "controller_log_available": log_path.exists(),
        "controller_log_sha256": digest.hexdigest() if log_path.exists() else None,
        "last_decision_s": last_decision_s, "decision_count": decision_count,
        "unfinished_transaction": bool(transaction), "plan_id": plan_id,
        "service_id": transaction.get("decision", {}).get("service_id"),
        "transaction_status": transaction.get("status"),
        "transaction_started_s": transaction_started_s,
        "uncertain_operation": uncertain.get("action", {}).get("op"),
        "pending_operation": command.get("action", {}).get("op"),
        "target_node_id": target,
        "target_last_script_state": last_node_change["attrs"].get("state") if last_node_change else None,
        "target_last_state_time_s": last_node_change["time_s"] if last_node_change else None,
        "unfinished_transaction_timeouts": dict(timeout_counts),
        "source_hashes": report["source_hashes"],
        "evidence_scope": "末态事务与控制日志；未根据吞吐曲线推断事务状态",
    }


def control_wait_section(records):
    """三组控制结果使用同一张表，区分已完成与长期未完成的事务。"""
    rows = []
    labels = {prefix: label for prefix, label, _, _ in SCENARIOS}
    for prefix, row in records.items():
        if row["unfinished_transaction"]:
            last_state = row["target_last_script_state"]
            target = row["target_node_id"] or "—"
            if last_state == "offline":
                target += f"；{number(row['target_last_state_time_s'], 1)} s离线"
            state = f"{row['plan_id']} / {row['service_id']}；{row['transaction_status']}"
            if row["uncertain_operation"] == "deactivate":
                state += "（停源未确认，持续查询）"
            timeouts = number(row["unfinished_transaction_timeouts"].get("query", 0), 0)
        else:
            target, state, timeouts = "—", "无未完成事务", "—"
        rows.append([labels[prefix], number(row["last_decision_s"], 1), target, state, timeouts])
    return ("<h2 id='control-wait'>持续离线后的控制等待</h2>"
            "<p>下表从各次运行的末态事务记录和控制日志中提取。最后决策时刻指最后一条路径决策记录的时间；"
            "控制报告接收和命令查询可以在此后继续发生。</p>" +
            table(["场景", "最后路径决策 / s", "等待目标", "运行结束时的事务", "该事务查询超时 / 次"], rows) +
            "<p>三套场景共用当前串行控制流程：有未完成命令或事务时，控制循环先等待、重试或查询，"
            "后续业务的诊断与路径决策暂不推进。持续离线节点无法返回停源确认时，这一等待会阻塞后续决策；"
            "已有业务规则仍可能继续发送，从而在失效链路处积压或丢包。</p>"
            "<p>该现象反映了当前串行控制流程的适用限制。后续可按业务隔离失联事务，明确重试上限与退出条件，"
            "并保留未确认资源的记录，避免一项业务的等待阻塞整个控制循环。</p>"
            "<p><a href='控制等待记录.json'>查看控制等待记录 JSON</a></p>")


def coverage_section(reports):
    """用实际业务需求和事件记录说明场景覆盖，区分登记、活动与传输结果。"""
    names = {'video': '视频', 'image': '图像', 'data': '数据', 'voice': '话音'}
    records = {}
    for prefix, _, _, _ in SCENARIOS:
        report = reports[f'{prefix}_fixed']
        added = {e['attrs']['serviceId'] for e in report['events'] if e['type'] == 'service_adjust'}
        records[prefix] = {
            'initial': Counter(s['type'] for s in report['services'] if s['id'] not in added),
            'initial_rates': {kind: sum(float(s['required_bps']) for s in report['services']
                                       if s['id'] not in added and s['type'] == kind) / 1e6
                              for kind in {s['type'] for s in report['services']}},
            'active': Counter(s['type'] for s in report['services'] if s['demand_bits'] > 0),
            'added': Counter(e['attrs']['serviceType'] for e in report['events'] if e['type'] == 'service_adjust'),
        }
    initial = records['official']['initial']
    types = list(dict.fromkeys(list(initial) + [kind for r in records.values() for kind in r['active']]))
    html = ["<h2 id='coverage'>业务需求与动态事件覆盖</h2>",
            "<p>场景覆盖从四个方面检查：初始需求是否纳入、业务类型和新增需求是否齐全、五项质量要求是否给定，以及节点和链路变化是否被触发。覆盖说明场景安排了什么；交付率与时延说明网络实际完成了多少。</p>",
            table(['覆盖维度', '检查依据', '自建规则对齐场景的设计'], [
                ['初始需求', '与选定官方20节点的40条初始业务逐项对应', '40条均从0秒提出需求，初始总需求141.759 Mbps'],
                ['业务类型及新增需求', '视频、图像、数据、话音；分别记录初始与动态新增业务', '四类初始需求全部纳入；每类动态新增1条，共44条需求'],
                ['业务质量要求', '带宽、时延、抖动、误码率、丢包率；另有端点和优先级', '继承所引用官方业务的完整参数，按共享链路容量竞争资源'],
                ['网络动态变化', '参数更新、业务新增、移动、节点离线/上线、链路通断', '21条事件覆盖5类核心事件；包括持续故障、链路恢复和备用卫星参数变化'],
            ]),
            "<h3>业务类型与数量</h3>"]
    rows = [[names.get(kind, kind), initial.get(kind, 0), number(records['official']['initial_rates'].get(kind, 0), 3),
             *(records[p]['active'].get(kind, 0) for p in ('test', 'official', 'aligned')),
             records['aligned']['added'].get(kind, 0)] for kind in types]
    rows.append(['合计', sum(initial.values()), number(sum(records['official']['initial_rates'].values()), 3),
                 *(sum(records[p]['active'].values()) for p in ('test', 'official', 'aligned')),
                 sum(records['aligned']['added'].values())])
    html.append(table(['业务类型', '共同初始定义 / 条', '初始配置需求 / Mbps', '自建测试：有需求 / 条',
                       '官方基准：有需求 / 条', '自建对齐：有需求 / 条', '自建对齐其中新增 / 条'], rows))
    html.append("<p>初始配置需求按40条业务的全量定义求和；自建测试场景通过活动窗只启用其中部分业务。“有需求”表示业务在仿真期间至少提出过一次正需求，包含动态新增业务；不要求同时活动，也不表示已被管控接纳或成功交付。官方初始数据已包含上述四类业务；自建规则对齐场景将动态新增业务由script-09中的单一图像扩展为四类，便于观察不同需求加入后的资源竞争。</p>")
    html.append("<h3>需求参数与判定依据</h3>" + table(['参数', '字段 / 单位', '含义及用途'], [
        ['带宽需求', 'bandwidthBps / bps', '期望获得的有效业务速率，用于发包需求、容量接纳及需求完成度计算'],
        ['时延门限', 'delayMs / ms', '允许的端到端时延上限，用于候选路径质量判断及实际按时交付统计'],
        ['抖动门限', 'jitterMs / ms', '允许的时延波动上限，参与候选路径的配置级质量判断'],
        ['误码率门限', 'ber / 比例', '允许的误码率上限，参与配置级质量判断；传输层未模拟逐比特差错'],
        ['丢包率门限', 'packetLossRate / 比例', '允许的丢包比例上限；实际丢包还可能由排队溢出、失联等原因产生'],
        ['优先级与端点', 'priority、srcNodeId、dstNodeId', '确定接纳次序及通信两端；每条业务由自己的源节点产生'],
    ]))
    example = next((e for e in reports['official_fixed']['events'] if e['type'] == 'service_adjust'), None)
    if example:
        attrs = example['attrs']
        requirement = attrs['requirement']
        html.append(f"<p>例如，官方基准场景在{example['time_s']:g}秒新增{names.get(attrs['serviceType'], attrs['serviceType'])}业务"
                    f"{escape(attrs['serviceId'])}（{escape(attrs['srcNodeId'])} → {escape(attrs['dstNodeId'])}）："
                    f"需求速率{requirement['bandwidthBps']/1e6:g} Mbps，时延≤{requirement['delayMs']:g} ms，"
                    f"抖动≤{requirement['jitterMs']:g} ms，误码率≤{requirement['ber']:g}，丢包率≤{requirement['packetLossRate']*100:g}%，"
                    f"优先级{attrs['priority']}。这些数值规定业务希望获得的服务质量，实际传输结果需从运行指标观察。</p>")
    html.append("<h3>动态事件覆盖</h3>")
    event_types = (
        ('link_param_change', '链路参数更新', '容量、时延、抖动、误码率和丢包率的配置更新'),
        ('service_adjust', '新增业务', '按时刻登记新的源端、目的端及业务需求'),
        ('node_move', '节点移动', '更新节点位置；链路质量由独立事件给出'),
        ('node_offline', '节点离线', '节点不可用，关联链路及端点业务受影响'),
        ('node_online', '节点上线', '离线节点重新加入网络'),
        ('link_state', '链路通断', '链路可用状态切换，区分中断与恢复'),
    )
    rows = []
    for kind, label, meaning in event_types:
        counts = []
        for prefix in ('test', 'official', 'aligned'):
            events = [e for e in reports[f'{prefix}_fixed']['events'] if e['type'] == kind]
            value = f'✓ {len(events)}次' if events else '— 未安排'
            if kind == 'link_state' and events:
                down = sum(e['attrs']['state'] == 'down' for e in events)
                up = sum(e['attrs']['state'] == 'up' for e in events)
                value += f'（中断{down} / 恢复{up}）'
            counts.append(value)
        rows.append([label, meaning, *counts])
    html.append(table(['事件类型', '具体变化', '自建测试', '官方基准', '自建规则对齐'], rows))
    html.append("<p>三套场景共享微波13条、5G 9条、光纤1条、卫星4条、VHF 2条和UHF 1条链路。官方基准的参数更新涉及微波与5G；自建规则对齐场景的12次参数更新涉及微波、5G、卫星、VHF和UHF，并通过通断事件观察光纤变化。每次参数更新同时给出容量、时延、抖动、误码率与丢包率，具体取值列于各场景的事件表。</p>")
    html.append("<p>自建规则对齐场景旨在扩展当前官方样本的业务需求组合与动态变化。其覆盖范围是所选样本的四类业务和上表所列事件；当前未安排节点重新上线。更多业务类型、节点规模和扰动组合可通过场景配置继续扩展。</p>")
    return ''.join(html)


def metric_guide():
    """在结果数值之前解释分母、统计对象与单位。"""
    return ("<h2 id='definitions'>评价指标与阅读方法</h2>"
            "<p>先用需求完成度判断业务保障程度，再结合交付速率、丢包、时延和队列定位瓶颈。业务需求由剧本给定；实际发包量同时受节点状态和管控接纳结果影响。</p>" +
            table(['指标', '计算方式 / 单位', '用于观察'], [
                ['平均业务需求', '业务期内各业务需求速率的时间积分之和 ÷ 业务期长度；Mbps', '场景要求网络承担多少业务。未接纳或端点离线期间的有效需求仍计入'],
                ['实际发包送达率', '已送达业务包数 ÷ 实际生成业务包数 × 100%', '进入传输流程的数据包有多少到达目的端；未获接纳而未生成的包不进入分母'],
                ['需求交付完成度', '已送达有效业务位数 ÷ 累计需求位数 × 100%', '全部有效需求得到满足的程度；包含未获接纳需求的影响'],
                ['需求按时完成度', '在所属业务时延门限内送达的有效位数 ÷ 累计需求位数 × 100%', '满足时延要求的需求占比；仅评价时延，不合并其他四项质量门限'],
                ['业务交付速率', '20秒窗口内已送达有效业务位数 ÷ 窗口长度；Mbps', '各事件发生前后实际送达速率的变化'],
                ['已收包平均时延', '成功送达业务包的端到端时延之和 ÷ 已收包数；ms', '成功传输的等待成本；丢弃包没有进入此均值'],
                ['终结丢包', '20秒窗口内已确定丢弃的业务包数；图中单位为千包', '排队溢出、链路随机丢失或失效等原因造成的损失'],
                ['全网等待队列采样峰值', '每秒汇总各链路等待包数，取20秒窗口内最大值；包', '全网积压程度；不包含正在发送和传播的包，采样间可能存在更高峰值'],
                ['结束时在途包数', '收尾结束时尚未交付、也未终结丢弃的业务包数；包', '仍滞留于队列或传输过程的业务包，单独列出'],
            ]) +
            "<p>例如累计需求为100 Mbit，管控只批准发送40 Mbit，其中39 Mbit送达：若分组等长，实际发包送达率为97.5%，需求交付完成度为39%。两项指标一起阅读，才能同时了解传输可靠性与需求保障程度。</p>")


def create_report(run_root, output):
    reports = {f"{prefix}_{mode}": read_run(run_root / f"{prefix}_{mode}")
               for prefix, _, _, _ in SCENARIOS for mode in ("fixed", "control")}
    comparable(reports)
    output.mkdir(parents=True, exist_ok=True)
    control_wait = {prefix: read_control_wait(run_root / f"{prefix}_control", reports[f"{prefix}_control"])
                    for prefix, _, _, _ in SCENARIOS}
    (output / "控制等待记录.json").write_text(json.dumps(control_wait, ensure_ascii=False,
        indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary_rows = []
    for prefix, label, _, filename in SCENARIOS:
        fixed, control = (reports[f"{prefix}_{mode}"] for mode in ("fixed", "control"))
        draw_scenario(fixed, control, label, output / filename)
        for mode, mode_label in (("fixed", "固定路由"), ("control", "规则管控")):
            r = reports[f"{prefix}_{mode}"]
            d = r["data"]
            summary_rows.append({"场景": label, "策略": mode_label,
                "平均业务需求_Mbps": d["demand_bits"] / (r["config"]["end_s"] - r["config"]["start_s"]) / 1e6,
                "发包数": d["generated"], "送达包数": d["delivered"], "终结丢包数": d["dropped"],
                "结束时在途包数": d["pending"], "实际发包送达率_%": d["delivery_pct"],
                "需求交付完成度_%": d["demand_delivery_pct"], "需求按时完成度_%": d["demand_timely_pct"],
                "已收包平均时延_ms": d["mean_delay_ms"], "全网等待队列采样峰值_包": r["telemetry"]["sampled_queue_peak"]})
    with (output / "三场景指标.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    (output / "三场景数据.json").write_text(json.dumps({"window_s": WINDOW_S, "metrics": summary_rows,
        "runs": reports}, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    html = ["""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>动态网络仿真：场景设计与运行评估</title><style>
body{font:16px/1.8 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;color:#243744;background:#f3f5f7;margin:0}
main{max-width:1200px;margin:28px auto;background:white;padding:32px 42px}h1{font-size:30px;color:#19364b;margin:0 0 12px}
h2{font-size:23px;margin-top:36px}h3{font-size:19px;margin-top:25px}p{margin:12px 0}a{color:#226d9c}nav{margin:20px 0}
table{border-collapse:collapse;width:100%;font-size:14px;margin:14px 0}th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid #dce3e8}
th{background:#eaf0f4;color:#19364b}tr:nth-child(even){background:#fafbfc}.table-wrap{overflow:auto}img{max-width:100%;height:auto}
.muted{color:#607282;font-size:14px}code{font:14px Consolas,monospace;word-break:break-word}summary{cursor:pointer;font-weight:bold}
@media(max-width:700px){main{padding:20px;margin:0}h1{font-size:25px}}@media print{body{background:white}main{margin:0;padding:0}img{break-inside:avoid}}
 </style><main><h1>动态网络仿真：场景设计与运行评估</h1>
<p>本系统模拟20节点异构通信网络中的业务产生、逐跳传输、有限队列和带内管控。业务与控制消息共同占用链路资源，网络随剧本中的业务新增、节点变化和链路扰动动态运行。</p>
<p>实验设置三套用途互补的场景：自建测试场景用于检查基础网络流程能否完整运行；官方基准场景用于建立赛事原始输入下的参考；自建规则对齐场景在官方初始网络和业务要求上进一步扩展，尽量纳入当前样本中的业务需求及多种动态变化。每套场景分别采用固定路由与规则管控，形成六组实际运行结果。</p>
<nav><a href="#purpose">场景用途</a> · <a href="#coverage">需求与事件覆盖</a> · <a href="#conditions">实验设置</a> · <a href="#definitions">指标说明</a> · <a href="#metrics">结果汇总</a> · <a href="#test">自建测试</a> · <a href="#official">官方基准</a> · <a href="#aligned">自建对齐</a> · <a href="#control-wait">控制等待</a> · <a href="#scope">模型范围</a></nav>
<h2 id="purpose">场景用途与设计</h2>"""]
    html.append(table(['场景', '目的', '主要观察内容'], [
        ['自建测试场景（EXT20-02）', '验证基础网络能否跑通', '在较低负载和分阶段活动下，观察业务发包、转发与排队、事件执行、带内管控和结果记录能否形成完整运行流程'],
        ['官方基准场景（20节点 script-09）', '建立官方输入下的运行参考', '沿用官方初始拓扑、40条业务及8条原始事件，观察完整需求下的资源竞争与动态响应'],
        ['自建规则对齐场景（EXT20-03）', '在官方场景基础上拓展需求与事件覆盖', '全部40条初始需求参与，四类业务各动态新增1条；组合多链路参数变化、节点移动、持续离线、中断和恢复过程'],
    ]))
    html.append('<p>三套场景分别服务于流程验证、官方输入对照和扩展评估。较低负载场景中的高送达率用于观察基础传输流程；在完整需求和故障条件下，还需结合需求完成度判断网络保障能力。</p>')
    html.append(coverage_section(reports))
    html.append('<h2 id="conditions">实验设置与管控策略</h2>')
    html.append(table(["项目", "自建测试场景", "官方基准场景", "自建规则对齐场景"], [
        ["场景编号", "EXT20-02", "sce20261010-conf02/sce01/script-09", "EXT20-03，自建扩展编号"],
        ["初始网络", "20节点、30链路", "同左", "同左"],
        ["初始业务", "40条定义，按活动窗启用；全程有17条初始业务参与", "40条全部从0秒提出需求", "40条全部从0秒提出需求"],
        ["初始活动需求", "约20.129～20.329 Mbps", "141.759 Mbps", "141.759 Mbps"],
        ["新增业务", "4条，按活动窗限时产生需求", "605秒新增图像5.4 Mbps", "图像5.4 Mbps、数据1.1 Mbps、话音78 kbps、视频6.2 Mbps"],
        ["需求持续", "按分阶段活动窗开始与停止", "新增后持续至业务期结束", "新增后持续至业务期结束"],
        ["事件安排", "23条事件，移动采用阶段采样", "8条官方事件，沿用其时刻与参数", "21条事件，分散安排显式扰动"],
    ]))
    ref = reports["official_control"]["config"]
    html.append(f"<p>共同设置：业务期 {ref['end_s']:g} 秒，收尾 {ref['drain_s']:g} 秒，种子 {ref['seed']}，业务包有效载荷 {ref['payload_bytes']} B，队列上限 {ref['queue_packets']} 包及 {ref['queue_bytes']} B，速率缩放 {ref['rate_scale']:g}。状态每秒采样，图表按20秒汇总。规则管控均启用 <code>qos_profile=official</code>；固定路由保持初始路径。</p>")
    html.append("<p>4200秒业务期与240秒收尾是三场景对照的统一实验设置。官方基准场景按原始时刻执行8条事件；全部有效需求按本项目活动约定持续至业务期结束。微波、5G、光纤、卫星、VHF和UHF六类链路共用同一分组传输模型。</p>")
    html.append(table(['策略', '运行方式', '主要观察目标'], [
        ['固定路由', '按初始链路配置计算路径并保持不变；活动业务在源端在线时按需求发包', '没有动态资源调整时，需求与网络变化对传输的影响'],
        ['规则管控', '接收经网络送达的报告，按优先级和剩余容量接纳业务；选择最小时延候选并检查五项配置质量，通过带内命令安装路径和速率', '基于有限观测的资源接纳与路径调整效果，以及消息传输和执行确认的影响'],
    ]))
    html.append("<p>首次官方扰动发生前的0～180秒内，官方基准场景与自建规则对齐场景在相同策略下的逐秒业务收发、丢包、收包时延总和及管控收发计数一致。该时段使用相同初始需求和网络条件；180秒后按各自事件历史演化。</p>")
    html.append(metric_guide())
    html.append("<h2 id='metrics'>运行结果汇总</h2>")
    html.append(table(["场景 / 策略", "平均需求 Mbps", "实际发包送达率 %", "需求交付完成度 %", "需求按时完成度 %", "已收包时延 ms", "结束在途 包"],
        [[r["场景"] + " / " + r["策略"], number(r["平均业务需求_Mbps"], 3), number(r["实际发包送达率_%"]),
          number(r["需求交付完成度_%"]), number(r["需求按时完成度_%"]), number(r["已收包平均时延_ms"]), number(r["结束时在途包数"], 0)] for r in summary_rows]))
    html.append("<p>规则管控可能暂不接纳部分业务，实际发包送达率因此可能很高。判断全部需求是否得到保障，应同时查看需求交付完成度和需求按时完成度。后两项仍将未获接纳或端点离线期间的有效需求计入分母。</p>")
    participation = []
    for prefix, label, _, _ in SCENARIOS:
        services = reports[f'{prefix}_control']['services']
        demanded = sum(s['demand_bits'] > 0 for s in services)
        sent = sum(s['generated'] > 0 for s in services)
        participation.append(f'{label}{demanded}条有需求业务中，{sent}条曾生成业务包')
    html.append('<p>规则管控运行记录：' + '；'.join(participation) + '。曾生成业务包只表示至少参与过一次发送，持续保障程度仍由需求完成度反映。</p>')
    html.append("<p><a href='三场景指标.csv'>下载完整指标表 CSV</a> · <a href='三场景数据.json'>查看指标、事件与时间曲线 JSON</a></p>")
    html.append('<p>两种策略应在同一场景内比较。三套场景的需求规模和事件过程不同，跨场景数值主要用于观察运行条件的影响。持续节点离线条件下，当前控制器存在等待确认而阻塞后续决策的现象，具体记录见后文“持续离线后的控制等待”。</p>')
    event_names = {"link_param_change": "链路参数", "service_adjust": "新增业务", "node_move": "节点移动", "node_offline": "节点离线", "node_online": "节点上线", "link_state": "链路通断"}
    purpose = {
        'test': '本场景用于验证基础网络能否跑通。分阶段业务活动和较低负载使发包、传输、事件执行与恢复过程更易观察；图中各曲线用于检查这些环节的响应，完整事件参数见图后表格。',
        'official': '本场景回放官方20节点script-09。40条初始需求与原始8条事件共同形成对照，便于观察初始资源竞争、图像业务加入、节点离线及链路中断对网络的影响。',
        'aligned': '本场景在官方初始网络和全部初始需求上进一步拓展。图像、数据、话音和视频分别在605、840、1500和2520秒加入，需求总速率由141.759 Mbps逐步达到154.537 Mbps；分散安排的链路与节点事件用于观察更丰富的运行条件。',
    }
    for prefix, label, _, filename in SCENARIOS:
        f, c = reports[f"{prefix}_fixed"], reports[f"{prefix}_control"]
        html.append(f"<h2 id='{prefix}'>{escape(label)}</h2>")
        html.append(f'<p>{escape(purpose[prefix])}</p>')
        gain = c["data"]["demand_delivery_pct"] - f["data"]["demand_delivery_pct"]
        html.append(f"<p>本场景实际执行 {len(f['events'])} 条核心事件。规则管控相对固定路由的需求交付完成度变化为 {gain:+.2f} 个百分点；两种策略结束时仍在途业务包分别为 {f['data']['pending']:,} 与 {c['data']['pending']:,} 个。</p>")
        encoded = base64.b64encode((output / filename).read_bytes()).decode("ascii")
        html.append(f"<img alt='{escape(label)}事件与四项网络运行曲线' src='data:image/png;base64,{encoded}'>")
        if prefix == "aligned":
            wait = control_wait[prefix]
            recovery = next((event for event in f["events"] if event["type"] == "link_state"
                             and event["attrs"].get("linkId") == "L00015"
                             and event["attrs"].get("state") == "up"), None)
            if (recovery and wait["unfinished_transaction"] and wait["last_decision_s"] is not None
                    and wait["last_decision_s"] < recovery["time_s"]):
                html.append(f"<p>{number(recovery['time_s'], 0)}秒执行L00015光纤恢复事件后，"
                            "已有业务规则重新获得该链路的传输条件。此时控制器仍在等待失联节点确认，"
                            "交付回升应归于光纤物理恢复。管控报文的主备切换仅调整管控路由，"
                            "该过程没有完成业务卫星接管或业务路径重算。</p>")
        if prefix == "test":
            html.append("<p>下表包含自建测试场景的业务活动窗切换和需求停止时刻；这些派生切换不计入核心事件图的圆点数量。</p>")
        html.append("<details><summary>业务活动与连通状态</summary><p>按实际执行批次显示状态变化，省略状态未变的重复行，并保留业务期结束行。活动需求数表示正在提出需求的业务数，与管控实际批准发包的数量分别统计。</p>")
        html.append(table(["时刻 s", "活动剖面", "活动需求数", "在线节点数", "可用链路数"],
                          activity_state_rows(f)) + "</details>")
        html.append("<details><summary>事件时刻与具体参数</summary>")
        rows = []
        for e in f["events"]:
            target, detail = event_description(e)
            rows.append([e["id"], number(e["time_s"], 0), event_names.get(e["type"], e["type"]), target, detail])
        html.append(table(["事件", "时刻 s", "类型", "对象", "执行内容"], rows) + "</details>")
        reasons = []
        for row, mode in ((f, "固定路由"), (c, "规则管控")):
            for reason, count in sorted(row["data"]["drop_reasons"].items(), key=lambda x: -x[1]):
                if count:
                    reasons.append([mode, DROP_NAMES.get(reason, reason), reason, f"{count:,}"])
        html.append("<details><summary>丢弃原因与数量</summary>" + table(["策略", "丢弃原因", "日志字段", "包数"], reasons) + "</details>")
    html.append(control_wait_section(control_wait))
    html.append("""<h2 id="scope">数据来源与模型范围</h2>
<ul><li>共同初始拓扑和40条初始业务来自官方sce20261010-conf02/sce01；官方基准场景的8条事件来自该目录script-09。</li>
<li>自建规则对齐场景的4条新增业务分别引用该官方场景下script-09、script-07、script-04、script-02中的图像、数据、话音、视频记录，采用其端点、需求和优先级。组合时刻、部分业务编号、链路扰动幅值与恢复时刻由项目设计，使用自建编号EXT20-03。</li>
<li>官方基准场景与自建规则对齐场景采用事件时刻更新目标坐标的共同移动约定。原始移动记录缺少持续时序；位置变化不会自动改变链路质量，链路参数变化由显式事件执行。自建测试场景保留原有移动采样。</li>
<li>每条链路双向共享容量和有限FIFO队列，各链路独立。BER参与配置质量检查，实际随机包丢失由PLR驱动；未模拟无线波形、跨链路干扰和MAC竞争。业务采用配置速率发包，未解码或播放真实媒体。</li>
<li>控制报告、命令及回执经过网络排队和传输。规则算法的计算耗时尚未注入仿真时间轴，现实执行耗时在单轮结果的performance.json中记录。</li></ul>
<p class="muted">本页图像已嵌入HTML，可离线阅读。CSV提供完整结果指标；JSON包含业务、事件、时间曲线和数据来源摘要。图后可展开查看活动状态、事件参数和丢弃原因。</p></main></html>""")
    target = output / "三场景运行对照.html"
    target.write_text("\n".join(html), encoding="utf-8")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="从三场景的六次完整运行生成对照报告")
    parser.add_argument("--run", type=Path, required=True, help="包含test/official/aligned六个运行目录的根目录")
    parser.add_argument("--output", type=Path, required=True, help="报告输出目录")
    args = parser.parse_args(argv)
    try:
        result = create_report(args.run.resolve(), args.output.resolve())
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        parser.exit(1, f"报告未生成：{error}\n")
    print(f"三场景运行报告：{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
