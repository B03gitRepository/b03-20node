"""赛题链路状态输出：保留 PDF 原拼写，并支持显式数据集兼容模式。

PDF 第4—5页使用 bandwitdhBps；数据集使用 bandwidthBps。
严格输出一次只采用一个键，不向官方对象加入 sim_time_us 等内部字段。
"""

from dataclasses import dataclass
import csv
import json
import math
from pathlib import Path
from typing import Literal

from resnet.contracts.validation import require_int
from .clock import ClockMapping

OutputProfile = Literal["pdf_compat", "dataset_compat"]
CAPACITY_KEYS = {"pdf_compat": "bandwitdhBps", "dataset_compat": "bandwidthBps"}
TOP_KEYS = {"linkId", "linkType", "srcNodeId", "dstNodeId", "status", "params", "time", "tsUs"}


def _finite(value: object, name: str, upper: float | None = None) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 必须为非负有限数值，不能是 bool")
    if upper is not None and value > upper:
        raise ValueError(f"{name} 不得大于 {upper}")
    return float(value)


@dataclass(frozen=True)
class LinkStatus:
    """运行状态与静态 LinkSpec 分离；单位转换只在边界发生。"""

    link_id: str
    link_type: str
    src_node_id: str
    dst_node_id: str
    status: str
    capacity_bps: int
    delay_s: float
    jitter_s: float
    ber: float
    packet_loss_rate: float
    sim_time_us: int

    def to_wire(self, clock: ClockMapping, profile: OutputProfile = "pdf_compat") -> dict:
        """返回经过字段和数值校验的 JSON 可序列化对象。"""
        if profile not in CAPACITY_KEYS:
            raise ValueError(f"未知输出模式: {profile}")
        require_int(self.capacity_bps, "capacity_bps", 1)
        _finite(self.delay_s, "delay_s")
        _finite(self.jitter_s, "jitter_s")
        _finite(self.ber, "ber", 1)
        _finite(self.packet_loss_rate, "packet_loss_rate", 1)
        obj = {
            "linkId": self.link_id,
            "linkType": self.link_type,
            "srcNodeId": self.src_node_id,
            "dstNodeId": self.dst_node_id,
            "status": self.status,
            "params": {
                "delayMs": self.delay_s * 1000,  # 内部秒转为赛题要求的毫秒。
                "jitterMs": self.jitter_s * 1000,
                CAPACITY_KEYS[profile]: self.capacity_bps,
                "ber": self.ber,
                "packetLossRate": self.packet_loss_rate,
            },
            **clock.fields(self.sim_time_us),  # 两个时间字段来自同一个整数时间源。
        }
        validate_wire(obj, clock, profile)
        return obj


def validate_wire(obj: dict, clock: ClockMapping, profile: OutputProfile = "pdf_compat") -> None:
    """检查字段集合、数值及时间一致性；当前项目状态范围为 up/down。"""
    if profile not in CAPACITY_KEYS:
        raise ValueError(f"未知输出模式: {profile}")
    if not isinstance(obj, dict) or set(obj) != TOP_KEYS:
        raise ValueError("链路状态顶层字段必须与赛题格式一致")
    for key in ("linkId", "linkType", "srcNodeId", "dstNodeId"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise ValueError(f"{key} 必须为非空字符串")
    if obj["srcNodeId"] == obj["dstNodeId"]:
        raise ValueError("本项目链路不能连接同一节点自身")
    if obj["status"] not in ("up", "down"):
        raise ValueError("本项目支持的链路状态为 up/down")
    key = CAPACITY_KEYS[profile]
    expected = {"delayMs", "jitterMs", key, "ber", "packetLossRate"}
    if not isinstance(obj["params"], dict) or set(obj["params"]) != expected:
        raise ValueError(f"params 字段不符合 {profile}，容量键必须为 {key}")
    params = obj["params"]
    require_int(params[key], key, 1)
    _finite(params["delayMs"], "delayMs")
    _finite(params["jitterMs"], "jitterMs")
    _finite(params["ber"], "ber", 1)
    _finite(params["packetLossRate"], "packetLossRate", 1)
    if obj["time"] != clock.format_timestamp(obj["tsUs"]):
        raise ValueError("time 与 tsUs 在配置时区下不一致")
    json.dumps(obj, allow_nan=False)  # 最后再阻止任何非标准 JSON 数值。


def export_statuses(statuses: list[LinkStatus], directory: Path, clock: ClockMapping,
                    profile: OutputProfile = "pdf_compat", *, sample_kind: str) -> dict:
    """导出 JSON、CSV、text 以及来源说明；sample_kind 必须明确样本性质。"""
    if profile not in CAPACITY_KEYS:
        raise ValueError(f"未知输出模式: {profile}")
    if not sample_kind.strip():
        raise ValueError("sample_kind 必须区分真实观测和配置快照")
    directory.mkdir(parents=True, exist_ok=True)
    records = [status.to_wire(clock, profile) for status in statuses]
    (directory / "link-status.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    capacity_key = CAPACITY_KEYS[profile]
    fields = ["linkId", "linkType", "srcNodeId", "dstNodeId", "status", "delayMs",
              "jitterMs", capacity_key, "ber", "packetLossRate", "time", "tsUs"]
    with (directory / "link-status.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({**{k: v for k, v in record.items() if k != "params"}, **record["params"]})
    # 每行一个合法 JSON 对象，扩展名 text 与赛题的文本输出类别对应。
    (directory / "link-status.text").write_text(
        "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records),
        encoding="utf-8")
    metadata = {"profile": profile, "sample_kind": sample_kind, "records": len(records),
                "json_container": "array_of_competition_records", "clock": clock.metadata(),
                "note": "CSV is a project flattening of the PDF nested JSON; PDF does not specify CSV columns."}
    (directory / "format-manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata
