"""管控消息契约，负责检查“报文长什么样”，不负责网络传输。

这里是探针和指令使用的统一表格。字段拼错、时间单位混淆、重复键，
都在进入决策或执行之前拒绝。编码后的完整报文由仿真网络逐跳排队传输。
输入：Python 字典或 UTF-8 JSON 字符串；输出：已检查的字典/紧凑JSON。
依赖：Python 标准库。时间全部使用整数 ns，不读取电脑墙钟。
"""
from __future__ import annotations

import json
import math
from typing import Any

from .validation import require_int

MAX_UDP_PAYLOAD_BYTES = 1472  # IPv4 MTU1500减去20B IP头和8B UDP头。
OBSERVATION_TTL_NS = 10_000_000_000
COMMAND_VALIDITY_NS = 5_000_000_000
ACTION_OPS = {"prepare", "install", "activate", "deactivate", "cleanup", "abort", "query"}


def require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须为非空字符串")
    return value


def _keys(value: Any, expected: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} 字段应为 {sorted(expected)}")


def _boolean(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} 必须为 bool")


def _probability(value: Any, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} 必须为 [0,1] 内的有限数")


def validate_probe(probe: dict) -> dict:
    """检查一条节点本地链路探针，返回同一字典；不修改字段或添加观测事实。"""
    _keys(probe, {"kind", "node_id", "seq", "observed_at_ns", "node_online", "version", "link"}, "probe")
    if probe["kind"] != "probe":
        raise ValueError("探针kind必须为probe")
    require_text(probe["node_id"], "node_id")
    for key in ("seq", "observed_at_ns", "version"):
        require_int(probe[key], key)
    _boolean(probe["node_online"], "node_online")
    link = probe["link"]
    _keys(link, {"link_id", "peer_node_id", "admin_up", "endpoint_online", "capacity_bps",
                 "delay_ns", "jitter_ns", "packet_loss_rate", "ber", "queue_packets"}, "probe.link")
    for key in ("link_id", "peer_node_id"):
        require_text(link[key], key)
    for key in ("admin_up", "endpoint_online"):
        _boolean(link[key], key)
    require_int(link["capacity_bps"], "capacity_bps", 1)
    for key in ("delay_ns", "jitter_ns", "queue_packets"):
        require_int(link[key], key)
    for key in ("packet_loss_rate", "ber"):
        _probability(link[key], key)
    return probe


def validate_command(command: dict) -> dict:
    """检查节点命令的结构与版本递增，操作的设备级合法性由本地执行器检查。

    一个命令只针对一个节点。不同节点的命令有各自回执，不能因为这个格式
    结构相同就假定它们在全网同时生效。query只查询，版本保持不变。
    """
    _keys(command, {"kind", "command_id", "plan_id", "target_node_id", "issued_at_ns",
                    "validity_ns", "base_version", "new_version", "action"}, "command")
    if command["kind"] != "command":
        raise ValueError("命令kind必须为command")
    for key in ("command_id", "plan_id", "target_node_id"):
        require_text(command[key], key)
    for key in ("issued_at_ns", "base_version", "new_version"):
        require_int(command[key], key)
    require_int(command["validity_ns"], "validity_ns", 1)
    action = command["action"]
    if not isinstance(action, dict) or action.get("op") not in ACTION_OPS:
        raise ValueError("action.op不受支持")
    allowed = {"op", "service_id", "epoch", "link_id", "next_node_id", "reserved_bps", "lease_until_ns", "demand_revision"}
    if not set(action) <= allowed:
        raise ValueError("action含未知字段")
    required = {"op", "service_id", "epoch"}
    if action["op"] in {"prepare", "install"}:
        required |= {"link_id", "next_node_id", "reserved_bps", "lease_until_ns"}
    elif action["op"] == "activate":
        required |= {"reserved_bps"}
    if not required <= set(action):
        raise ValueError(f"{action['op']}缺少必要字段：{sorted(required - set(action))}")
    require_text(action["service_id"], "service_id")
    require_int(action["epoch"], "epoch")
    for key in ("link_id", "next_node_id"):
        if key in action:
            require_text(action[key], key)
    for key in ("reserved_bps", "lease_until_ns", "demand_revision"):
        if key in action:
            require_int(action[key], key)
    expected = command["base_version"] + (action["op"] != "query")
    if command["new_version"] != expected:
        raise ValueError("写命令new_version应为base_version+1，query保持版本不变")
    return command


def command_precheck(command: dict, *, now_ns: int, target_node_id: str,
                     current_version: int) -> str | None:
    """按目标、时效、版本顺序检查；None表示可继续设备级检查，并非已执行。

    指令恰好在有效期末到达允许继续，超过才拒绝。重复command_id的幂等处理
    由执行器保存的历史回执完成，应在再次执行前处理，不能重新生效。
    """
    validate_command(command)
    require_int(now_ns, "now_ns")
    require_int(current_version, "current_version")
    if command["target_node_id"] != target_node_id:
        return "target_mismatch"
    if now_ns < command["issued_at_ns"]:
        return "future_command"
    if now_ns - command["issued_at_ns"] > command["validity_ns"]:
        return "expired_command"
    # 查询用于恢复远端版本认知，不写状态，因此允许不知道当前版本时查询。
    if command["action"]["op"] != "query" and command["base_version"] != current_version:
        return "version_conflict"
    return None


def encode_message(message: dict, *, max_payload_bytes: int = MAX_UDP_PAYLOAD_BYTES) -> str:
    """生成实际放入UDP的紧凑JSON。按UTF-8字节数检查MTU，不能按汉字字符数估算。"""
    if not isinstance(message, dict):
        raise ValueError("消息必须为dict")
    value = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > max_payload_bytes:
        raise ValueError("消息超过UDP负载上限；应拆成独立报文并分别计入带宽")
    return value


def decode_message(payload: str, *, max_payload_bytes: int = MAX_UDP_PAYLOAD_BYTES) -> dict:
    """解码网络实际收到的JSON，拒绝重复字段、NaN/Infinity和非对象顶层。"""
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > max_payload_bytes:
        raise ValueError("消息类型或长度不符")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复JSON字段：{key}")
            result[key] = value
        return result
    def reject(value):
        raise ValueError(f"不支持的JSON数值：{value}")
    message = json.loads(payload, object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(message, dict):
        raise ValueError("消息顶层必须为JSON对象")
    def finite(value):
        if isinstance(value, dict):
            for child in value.values(): finite(child)
        elif isinstance(value, list):
            for child in value: finite(child)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("消息含超出有限范围的浮点数")
    finite(message)  # 也拦截合法JSON指数1e999解析后变为Infinity的情况。
    return message
