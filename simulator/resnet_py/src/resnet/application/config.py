"""实验配置读取：选择运行案例，不创建或调度场景事件。

输入配置文件与 case_id，返回场景、案例参数、场景目录和配置摘要。
场景事件只由 io.load_scenario 读取；这里的 cases 表示不同的运行方式。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from resnet.io import load_scenario
from resnet.simulation.simulation import DEFAULT_CONFIG

SCHEMA = "resnet-experiment-v1"
ROOT_KEYS = {"schema", "extends", "scenario_dir", "cases", "simulation"}


def _object(value, location):
    if not isinstance(value, dict):
        raise ValueError(f"{location} 必须是 JSON 对象")
    return value


def _text(value, location):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} 必须是非空字符串")
    return value


def _unknown(value, allowed, location):
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"{location} 包含未知字段：{sorted(extra)}")


def _read_json(path):
    """按同一份字节解析并记录摘要，拒绝重复字段及非有限数值。"""
    raw = path.read_bytes()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{path.name} 包含重复 JSON 字段：{key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"{path.name} 含非有限数值：{value}")

    def finite(value):
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)
        elif isinstance(value, float) and not math.isfinite(value):
            reject(value)

    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=reject)
    _object(data, str(path))
    finite(data)
    if data.get("schema") == "resnet-pure-python-v1":
        raise ValueError("旧实验配置需要升级：使用 configs/experiment.json，schema 改为 "
                         f"{SCHEMA}，并将 purpose 说明移到文档")
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}.schema 应为 {SCHEMA}")
    _unknown(data, ROOT_KEYS, path.name)
    return data, hashlib.sha256(raw).hexdigest()


def _options(value, location):
    """检查每个参数块，包括未被本次选中的案例；语义边界由仿真层继续检查。"""
    _object(value, location)
    _unknown(value, set(DEFAULT_CONFIG), location)
    for key, item in value.items():
        name = f"{location}.{key}"
        if key == "control":
            _object(item, name)
        elif key == "mode":
            if item not in ("fixed", "control", "marl"):
                raise ValueError(f"{name} 必须为 fixed/control/marl")
        elif key == "log_control":
            if type(item) is not bool:
                raise ValueError(f"{name} 必须为布尔值")
        elif key == "service_ids":
            if item is not None and (not isinstance(item, list)
                    or any(not isinstance(sid, str) or not sid for sid in item)
                    or len(set(item)) != len(item)):
                raise ValueError(f"{name} 必须为不重复的业务编号列表或 null")
        elif key in ("seed", "queue_packets", "queue_bytes", "payload_bytes", "sample_modulus"):
            minimum = 0 if key in ("seed", "sample_modulus") else 1
            if type(item) is not int or item < minimum:
                raise ValueError(f"{name} 必须为不小于 {minimum} 的整数")
        elif key == "end_s" and item is None:
            pass  # null 明确表示使用所选场景的完整时长。
        elif type(item) not in (int, float) or not math.isfinite(item) or item < 0:
            raise ValueError(f"{name} 必须为有限非负数值")


def _merge_options(base, override):
    """一般字段由后者覆盖；control 的单项参数合并，列表始终整体替换。"""
    result = deepcopy(base) | deepcopy(override)
    if "control" in base or "control" in override:
        result["control"] = deepcopy(base.get("control", {})) | deepcopy(override.get("control", {}))
    return result


def _resolve(path, stack, hashes):
    path = path.resolve()
    if path in stack:
        chain = " -> ".join(p.name for p in (*stack, path))
        raise ValueError(f"实验配置 extends 存在循环引用：{chain}")
    data, digest = _read_json(path)
    # 顺序为当前文件到父文件；摘要同时覆盖继承配置，父文件修改也会改变摘要。
    hashes.append(digest)
    base = {}
    if "extends" in data:
        parent = path.parent / _text(data["extends"], f"{path.name}.extends")
        base = _resolve(parent, (*stack, path), hashes)
    if "simulation" in data:
        _options(data["simulation"], f"{path.name}.simulation")
    if "cases" in data:
        if not isinstance(data["cases"], list) or not data["cases"]:
            raise ValueError(f"{path.name}.cases 必须是非空列表")
        seen = set()
        for index, case in enumerate(data["cases"]):
            location = f"{path.name}.cases[{index}]"
            _object(case, location)
            case_id = _text(case.get("case_id"), f"{location}.case_id")
            if case_id in seen:
                raise ValueError(f"{path.name} 存在重复 case_id：{case_id}")
            seen.add(case_id)
            _options({k: v for k, v in case.items() if k != "case_id"}, location)
    result = deepcopy(base) | deepcopy(data)
    result["simulation"] = _merge_options(base.get("simulation", {}), data.get("simulation", {}))
    if "scenario_dir" in data:
        # 相对路径始终以声明该字段的文件为基准，避免放入 examples 后路径改变。
        result["scenario_dir"] = (path.parent / _text(data["scenario_dir"],
                                                       f"{path.name}.scenario_dir")).resolve()
    return result


def load_config(path, case_id):
    """返回 (ScenarioSpec, 参数字典, 场景目录, 配置SHA-256)。

    扩展方式：复制 experiment.json 修改 scenario_dir；短窗口可用 extends
    继承公共参数并替换整个 cases 列表。新增参数还须在仿真层实现并验证，
    仅向 JSON 增加字段会被拒绝。此函数不会执行 cases 中未选中的案例。
    """
    hashes = []
    data = _resolve(Path(path), (), hashes)
    if "scenario_dir" not in data or "cases" not in data:
        raise ValueError("实验配置必须提供 scenario_dir 和 cases，或从 extends 继承")
    cases = {case["case_id"]: case for case in data["cases"]}
    if case_id not in cases:
        raise ValueError(f"未知运行案例 {case_id!r}；可选：{list(cases)}")
    source = data["scenario_dir"]
    scenario = load_scenario(source)
    # 控制参数即使在 fixed 案例中暂不启用，也拒绝拼错字段和无效引用。
    from resnet.control.config import control_defaults
    service_ids = {s.service_id for s in scenario.services + scenario.added_services}
    node_ids = {n.node_id for n in scenario.nodes}
    for case in cases.values():
        effective = _merge_options(data.get("simulation", {}),
                                   {k: v for k, v in case.items() if k != "case_id"})
        control = effective.get("control", {})
        requires_controller = effective.get("mode", DEFAULT_CONFIG["mode"]) == "control" or "controller_id" in control
        control_defaults(control, service_ids=service_ids, node_ids=node_ids if requires_controller else ())
    selected = {k: v for k, v in cases[case_id].items() if k != "case_id"}
    config = _merge_options(data.get("simulation", {}), selected)
    digest = hashes[0] if len(hashes) == 1 else hashlib.sha256(
        json.dumps(hashes, separators=(",", ":")).encode("ascii")).hexdigest()
    return scenario, config, source, digest
