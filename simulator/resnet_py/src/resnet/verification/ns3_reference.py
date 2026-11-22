"""用保存的 ns-3 实测记录验证仿真链路模型；运行时不需要安装 ns-3。

把旧网络真正收到的“包在什么时候进入链路”保存成输入，再交给新链路。
随后逐包核对入队、开始发送、完成发送、到达或丢弃时刻。期望结果来自旧日志，
不是新程序自己算出的答案。首次 --extract-from 用于从只读原工程制作便携夹具。

运行：python scripts/compare_ns3_reference.py --output runs/reference-validation-01
重建：加 --extract-from ../resnet（只读取原项目，不重新运行 ns-3）。
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil

PROJECT = Path(__file__).resolve().parents[3]

import simpy

from resnet.simulation.engine import Call, advance_inclusive
from resnet.simulation.link import ResourceLink
from resnet.simulation.packet import Packet


CASES = ("single_saturation", "bidirectional_saturation", "outage_recovery")
COLUMNS = ["tx_id", "direction", "offered_at_ns", "wire_bytes", "enqueue_ns", "start_ns",
           "finish_ns", "deliver_ns", "drop_ns", "drop_reason"]
STAGES = {"enqueue": 4, "start": 5, "finish": 6, "deliver": 7, "drop": 8}
REFERENCE = PROJECT / "reference" / "ns3_deterministic"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract(original: Path):
    """只提取实测字段并保存来源哈希；原逐包日志数 MB，不全部重复打包。"""
    old_config = original / "configs/resource_link_suite.json"
    suite = json.loads(old_config.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in suite["cases"]}
    REFERENCE.mkdir(parents=True, exist_ok=True)
    provenance = dict(schema="resnet-ns3-reference-manifest-v1", columns=COLUMNS,
        source_config=dict(path="configs/resource_link_suite.json", sha256=sha(old_config)),
        source_run="runs/s4-validation-20261114-02", cases=[],
        scope="deterministic_resource_link_replay_not_random_full_network_equivalence",
        origin="原项目自定义S4验收案例的ns-3实测；不是竞赛官方业务脚本")
    shutil.copyfile(old_config, REFERENCE / "resource_link_suite.json")
    for case_id in CASES:
        source = original / provenance["source_run"] / case_id
        destination = REFERENCE / case_id
        destination.mkdir(parents=True, exist_ok=True)
        config = cases[case_id]
        # 随机结果不能用相同 seed 声称跨引擎逐包一致；夹具明确拒绝随机案例。
        if config["link"]["jitter_ms"] or config["link"]["packet_loss_rate"]:
            raise ValueError("此比较仅支持无抖动、无随机损失的案例")
        jobs = {}
        with (source / "resource-link.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                tid = row["tx_id"]
                if not tid:
                    continue
                if tid not in jobs:
                    # enqueue_ns 在队满拒收时也保留原尝试时刻；stage字段留空表示未发生。
                    jobs[tid] = [tid, row["direction"], row["enqueue_ns"], row["wire_bytes"],
                                 None, None, None, None, None, ""]
                if row["event"] in STAGES:
                    index = STAGES[row["event"]]
                    if jobs[tid][index] is not None:
                        raise ValueError("参考日志同一任务重复出现同一阶段")
                    jobs[tid][index] = row["time_ns"]
                    if row["event"] == "drop":
                        jobs[tid][9] = row["reason"]
        if sorted(jobs) != list(range(1, len(jobs)+1)):
            raise ValueError("参考任务编号存在缺口")
        packet_file = destination / "packets.jsonl"
        packet_file.write_text("".join(json.dumps(jobs[k], separators=(",", ":"))+"\n"
                                      for k in sorted(jobs)), encoding="utf-8")
        copied = {}
        for name in ("resolved-config.json", "summary.json", "runtime.json", "flow-monitor.xml"):
            shutil.copyfile(source / name, destination / name)
            copied[name] = sha(destination / name)
        provenance["cases"].append(dict(case_id=case_id, offered_packets=len(jobs),
            original_trace=dict(path=f'{provenance["source_run"]}/{case_id}/resource-link.jsonl',
                                sha256=sha(source / "resource-link.jsonl")),
            fixture_sha256=sha(packet_file), retained_files=copied))
    save(REFERENCE / "manifest.json", provenance)


def replay(case_record, output):
    """仅从输入列调度到包；不读取期望 start/finish/deliver 来驱动仿真。"""
    case_id = case_record["case_id"]
    directory = REFERENCE / case_id
    packet_file = directory / "packets.jsonl"
    if sha(packet_file) != case_record["fixture_sha256"]:
        raise ValueError(f"{case_id} 夹具哈希不匹配")
    for name, expected_hash in case_record["retained_files"].items():
        if sha(directory / name) != expected_hash:
            raise ValueError(f"{case_id}/{name} 参考文件哈希不匹配")
    config = json.loads((directory / "resolved-config.json").read_text(encoding="utf-8"))
    original_summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    runtime = json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
    expected = [json.loads(line) for line in packet_file.read_text(encoding="utf-8").splitlines()]
    actual = {r[0]: r[:4] + [None]*5 + [""] for r in expected}
    arrivals_by_direction = [[r[:4] for r in expected if r[1] == direction] for direction in (0, 1)]
    env = simpy.Environment()
    received = Counter()
    dropped = Counter()
    duplicate_stages = []

    def trace(row):
        if row["event"] not in STAGES:
            return
        # packet_uid 由夹具 tx_id 重编号，不要求两种引擎的原生 Packet UID 相同。
        record = actual[row["packet_uid"]]
        index = STAGES[row["event"]]
        if record[index] is not None:
            duplicate_stages.append((row["packet_uid"], row["event"]))
        record[index] = row["time_ns"]
        if row["event"] == "drop":
            record[9] = row["reason"]

    params = {key: config["link"][key] for key in ResourceLink.PARAMETER_NAMES}
    channel = ResourceLink(env, "S4_LINK_01", ("A", "B"), params,
        config["link"]["queue_packets"], config["link"]["queue_bytes"], 73,
        lambda packet, peer, lid: received.update(["forward" if peer == "B" else "reverse"]),
        lambda packet, reason, lid: dropped.update([reason]), trace)
    # 原运行器先登记故障，再安装业务；优先执行场景事件保持这一约定。
    for event in config["events"]:
        at_ns = round(event["time_s"]*1e9)
        if event["kind"] in ("down", "up"):
            Call(env, at_ns, lambda e=event: channel.set_up(e["kind"] == "up"), priority=0)
        elif event["kind"] == "update":
            if event["params"].get("jitter_ms", 0) or event["params"].get("packet_loss_rate", 0):
                raise ValueError("不允许随机更新进入精确对照")
            Call(env, at_ns, lambda e=event: channel.update_parameters(**e["params"]), priority=0)
        else:
            raise ValueError("未知参考事件")

    def emit(direction, index):
        row = arrivals_by_direction[direction][index]
        tid, _, when, wire_bytes = row
        source, destination = ("A", "B") if direction == 0 else ("B", "A")
        packet = Packet(tid, source, destination, wire_bytes-28, wire_bytes, when,
                        service_id="forward" if direction == 0 else "reverse")
        channel.put(packet, source)
        # UdpClient 每发一个包才安排下一个包。按方向递归安排可保留同刻事件次序，
        # 预先登记全部到包会改变“队满到包与发送完成恰好同刻”时的先后关系。
        next_index = index+1
        if next_index < len(arrivals_by_direction[direction]):
            next_time = arrivals_by_direction[direction][next_index][2]
            Call(env, next_time, lambda: emit(direction, next_index))

    for direction, rows in enumerate(arrivals_by_direction):
        if rows:
            Call(env, rows[0][2], lambda d=direction: emit(d, 0))
    advance_inclusive(env, runtime["simulation_stop_ns"])
    comparisons, mismatches = 0, []
    for row in expected:
        measured = actual[row[0]]
        for index in range(4, len(COLUMNS)):
            comparisons += 1
            if measured[index] != row[index]:
                mismatches.append(dict(tx_id=row[0], field=COLUMNS[index],
                                       expected=row[index], actual=measured[index]))
    stats = channel.snapshot()
    counts = dict(offered=stats["attempts"], started=stats["tx_started"], finished=stats["tx_finished"],
                  delivered=stats["rx"], dropped=stats["drops"], pending=stats["pending_count"],
                  drop_reasons=stats["drop_reasons"])
    application_expected = original_summary["app_received_packets"]
    actual_application = {key: received[key] for key in application_expected}
    checks = dict(per_packet_stage_times_and_reasons_equal=not mismatches,
        no_duplicate_stages=not duplicate_stages,
        aggregate_counts_equal=counts == original_summary["counts"],
        application_delivery_counts_equal=actual_application == application_expected,
        all_packets_accounted=stats["attempts"] == stats["rx"]+stats["drops"]+stats["pending_count"],
        queue_packet_peak_equal=stats["queue_peak_packets"] == original_summary["queue"]["peak_waiting_packets"],
        queue_byte_peak_equal=stats["queue_peak_bytes"] == original_summary["queue"]["peak_waiting_bytes"],
        max_wait_equal=stats["queue_wait_max_ns"] == round(original_summary["queue"]["max_wait_ms"]*1e6))
    result = dict(case_id=case_id, checks=checks, checks_passed=all(checks.values()),
        offered_packets=len(expected), compared_fields_per_packet=6, field_comparisons=comparisons,
        counts=counts, application_delivery_counts=actual_application,
        mismatches=mismatches[:20], mismatch_count=len(mismatches),
        fixture_sha256=case_record["fixture_sha256"],
        input_scope="原ns-3实际到包时间/方向/字节；每方向递归调度下一包",
        comparison_scope="逐包入队/开始/发送完成/到达/丢弃时刻及原因；纳秒精确，无随机损失或抖动")
    save(output / f"{case_id}.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-from", type=Path, help="只读原ns-3工程；省略时使用随包携带的夹具")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.extract_from:
        extract(args.extract_from.resolve())
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("请使用新的结果目录，避免覆盖验收记录")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((REFERENCE / "manifest.json").read_text(encoding="utf-8"))
    if sha(REFERENCE / "resource_link_suite.json") != manifest["source_config"]["sha256"]:
        raise ValueError("参考总配置哈希不匹配")
    results = [replay(record, args.output) for record in manifest["cases"]]
    summary = dict(schema="resnet-ns3-comparison-v1", checks_passed=all(r["checks_passed"] for r in results),
        cases=results, total_offered_packets=sum(r["offered_packets"] for r in results),
        total_field_comparisons=sum(r["field_comparisons"] for r in results),
        source_manifest_sha256=sha(REFERENCE / "manifest.json"),
        limitations=["使用既有ns-3实测证据，本次没有重新运行ns-3",
                     "仅验证保存的确定性资源链路案例，不代表完整随机EXT20逐包等价",
                     "输入来自链路入口实际到包，未重新验证IP协议栈及ns-3源应用实现"])
    save(args.output / "summary.json", summary)
    print(json.dumps({"checks_passed": summary["checks_passed"],
                      "cases": [{"case_id": r["case_id"], "passed": r["checks_passed"],
                                 "mismatches": r["mismatch_count"]} for r in results],
                      "total_field_comparisons": summary["total_field_comparisons"]}, ensure_ascii=False))
    return 0 if summary["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
