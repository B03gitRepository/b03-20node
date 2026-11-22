"""读取竞赛格式和项目扩展，生成唯一、经过校验的场景输入。"""

from collections import defaultdict
from copy import deepcopy
import hashlib
import ipaddress
import json
import math
from pathlib import Path
from typing import Any

from ..contracts import EventBatch, EventSpec, LinkSpec, NodeSpec, ScenarioSpec, ServiceSpec
from .validation import array, capacity, fail, finite_tree, mapping, microseconds, number, string


class _Reader:
    """集中读取，保证原始文档与 SHA-256 摘要对应同一份字节。"""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.documents: dict[str, Any] = {}
        self.hashes: dict[str, str] = {}

    def read(self, name: str) -> Any:
        name = string(name, "scenario-config.json", "files", "scenario")
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory):
            fail(name, "$", "file", "文件路径必须位于场景目录内")
        key = path.relative_to(self.directory).as_posix()
        if key in self.documents:
            return self.documents[key]
        try:
            data = path.read_bytes()
        except OSError as exc:
            fail(key, "$", "file", f"无法读取文件：{exc}")

        def unique_pairs(pairs):
            # JSON 的重复键会被默认解析器覆盖，此处明确拒绝。
            result = {}
            for k, v in pairs:
                if k in result:
                    fail(key, f"$.{k}", "JSON object", "JSON 键重复")
                result[k] = v
            return result

        try:
            parsed = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(key, "$", "file", f"不是有效的 UTF-8 JSON：{exc}")
        finite_tree(parsed, key)  # 包括尚未使用的元数据，禁止 NaN 与 Infinity。
        self.documents[key] = parsed
        self.hashes[key] = hashlib.sha256(data).hexdigest()
        return parsed


def _unique(value, seen: set, file: str, field: str) -> str:
    value = string(value, file, field)
    if value in seen:
        fail(file, field, value, "编号重复")
    seen.add(value)
    return value


def _reference(value, known: set, file: str, field: str, obj: str) -> str:
    value = string(value, file, field, obj)
    if value not in known:
        fail(file, field, obj, f"引用不存在的对象 {value}")
    return value


def _position(value, file: str, field: str, obj: str) -> dict[str, float]:
    value = mapping(value, file, field, obj)
    return {axis: float(number(value.get(axis), file, f"{field}.{axis}", obj, minimum=-math.inf))
            for axis in ("x", "y")}


def _qos(value, file: str, field: str, obj: str, warnings: list[str], *, partial=False) -> dict:
    value = mapping(value, file, field, obj)
    output = {}
    if not partial or any(k in value for k in ("bandwidthBps", "bandwitdhBps")):
        output["bps"] = capacity(value, file, field, obj, warnings)
    for name in ("delayMs", "jitterMs", "ber", "packetLossRate"):
        if not partial or name in value:
            output[name] = number(value.get(name), file, f"{field}.{name}", obj,
                                  maximum=1 if name in ("ber", "packetLossRate") else None)
    if partial and not output:
        fail(file, field, obj, "链路参数更新至少应给出五项链路参数中的一项")
    return output


def _nodes(document: dict, file: str) -> tuple[NodeSpec, ...]:
    ids, addresses, output = set(), set(), []
    for i, item in enumerate(array(document.get("nodes"), file, "nodes")):
        field = f"nodes[{i}]"
        item = mapping(item, file, field)
        nid = _unique(item.get("nodeId"), ids, file, f"{field}.nodeId")
        node_type = string(item.get("nodeType"), file, f"{field}.nodeType", nid)
        address = _unique(item.get("ip"), addresses, file, f"{field}.ip")
        mask = string(item.get("mask"), file, f"{field}.mask", nid)
        try:
            ipaddress.IPv4Address(address)
            ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
        except ValueError as exc:
            fail(file, f"{field}.ip/mask", nid, f"IPv4 地址或掩码不合法：{exc}")
        output.append(NodeSpec(nid, node_type, address, mask,
                               _position(item.get("position"), file, f"{field}.position", nid), deepcopy(item)))
    if not output:
        fail(file, "nodes", "topology", "至少需要一个节点")
    return tuple(output)


def _links(document, file, node_ids, warnings) -> tuple[LinkSpec, ...]:
    ids, output = set(), []
    for i, item in enumerate(array(document.get("links"), file, "links")):
        field = f"links[{i}]"
        item = mapping(item, file, field)
        lid = _unique(item.get("linkId"), ids, file, f"{field}.linkId")
        src = _reference(item.get("srcNodeId"), node_ids, file, f"{field}.srcNodeId", lid)
        dst = _reference(item.get("dstNodeId"), node_ids, file, f"{field}.dstNodeId", lid)
        if src == dst:
            fail(file, field, lid, "链路两端不能是同一节点")
        qos = _qos(item.get("params"), file, f"{field}.params", lid, warnings)
        # 按 linkId 保存每一条边；同一对节点的微波、卫星等并行链路不会互相覆盖。
        output.append(LinkSpec(lid, string(item.get("linkType"), file, f"{field}.linkType", lid),
                               src, dst, qos["bps"], qos["delayMs"] / 1000, qos["jitterMs"] / 1000,
                               qos["ber"], qos["packetLossRate"], deepcopy(item)))
    return tuple(output)


def _service(item, file, field, node_ids, seen, warnings, registered_at_us=None) -> ServiceSpec:
    item = mapping(item, file, field)
    sid = _unique(item.get("serviceId"), seen, file, f"{field}.serviceId")
    src = _reference(item.get("srcNodeId"), node_ids, file, f"{field}.srcNodeId", sid)
    dst = _reference(item.get("dstNodeId"), node_ids, file, f"{field}.dstNodeId", sid)
    if src == dst:
        fail(file, field, sid, "业务源宿不能是同一节点")
    kind = string(item.get("serviceType"), file, f"{field}.serviceType", sid)
    category = item.get("category")
    if category is not None:
        string(category, file, f"{field}.category", sid)
    priority = number(item.get("priority"), file, f"{field}.priority", sid, integer=True)
    # 新增业务的登记时刻由事件给出，不猜测其缺少的 category/startAtSec。
    if registered_at_us is None:
        registered_at_us = microseconds(item.get("startAtSec"), file, f"{field}.startAtSec", sid)
    qos = _qos(item.get("requirement"), file, f"{field}.requirement", sid, warnings)
    return ServiceSpec(sid, kind, category, src, dst, priority, qos["bps"], qos["delayMs"] / 1000,
                       qos["jitterMs"] / 1000, qos["ber"], qos["packetLossRate"],
                       registered_at_us, deepcopy(item))


def _events(document, file, node_ids, link_ids, service_ids, warnings, duration_us):
    output, added, previous = [], [], -1
    supported = {"link_param_change", "link_state", "node_move", "node_offline", "node_online", "service_adjust"}
    for i, item in enumerate(array(document, file, "$")):
        eid, field = f"E{i + 1:02d}", f"$[{i}]"
        item = mapping(item, file, field, eid)
        time = microseconds(item.get("time"), file, f"{field}.time", eid)
        if not previous <= time <= duration_us:
            fail(file, f"{field}.time", eid, "事件须按时刻非递减排列，且不超过仿真结束时刻")
        previous = time
        kind = string(item.get("eventType"), file, f"{field}.eventType", eid)
        if kind not in supported:
            fail(file, f"{field}.eventType", eid, f"不支持事件类型 {kind}")
        attrs = mapping(item.get("attrs"), file, f"{field}.attrs", eid)
        af = f"{field}.attrs"
        if kind.startswith("link_"):
            _reference(attrs.get("linkId"), link_ids, file, f"{af}.linkId", eid)
        if kind.startswith("node_"):
            _reference(attrs.get("nodeId"), node_ids, file, f"{af}.nodeId", eid)
        if kind == "link_param_change":
            _qos(attrs, file, af, eid, warnings, partial=True)
        elif kind == "link_state":
            if attrs.get("state") not in ("up", "down"):
                fail(file, f"{af}.state", eid, "链路状态只接受 up/down")
        elif kind in ("node_offline", "node_online"):
            expected = "offline" if kind == "node_offline" else "online"
            if attrs.get("state") != expected:
                fail(file, f"{af}.state", eid, f"此事件的 state 应为 {expected}")
        elif kind == "node_move":
            for key in ("fromX", "fromY", "toX", "toY"):
                number(attrs.get(key), file, f"{af}.{key}", eid, minimum=-math.inf)
        elif kind == "service_adjust":
            if attrs.get("op") != "add":
                fail(file, f"{af}.op", eid, "当前仅支持新增业务 add，其他操作需扩展事件处理器")
            added.append(_service(attrs, file, af, node_ids, service_ids, warnings, time))
        output.append(EventSpec(eid, kind, time, i, deepcopy(attrs), deepcopy(item)))
    return tuple(output), tuple(added)


def _movement_samples(config, events, node_ids, duration_us):
    file = "scenario-config.json"
    by_id = {event.event_id: event for event in events}
    samples = defaultdict(list)
    seen, used_triggers, intervals = set(), set(), defaultdict(list)
    for i, movement in enumerate(array(config.get("movements", []), file, "movements")):
        field = f"movements[{i}]"
        movement = mapping(movement, file, field)
        trigger = _reference(movement.get("trigger_event"), set(by_id), file, f"{field}.trigger_event", field)
        if trigger in used_triggers:
            fail(file, field, trigger, "移动触发事件只能绑定一段轨迹")
        used_triggers.add(trigger)
        event = by_id[trigger]
        nid = _reference(movement.get("node_id"), node_ids, file, f"{field}.node_id", trigger)
        start = microseconds(movement.get("start_s"), file, f"{field}.start_s", nid)
        end = microseconds(movement.get("end_s"), file, f"{field}.end_s", nid)
        if event.event_type != "node_move" or event.attrs["nodeId"] != nid or event.time_us != start:
            fail(file, field, trigger, "移动扩展与触发事件的类型、节点或起始时间不一致")
        if not start < end <= duration_us:
            fail(file, field, nid, "移动时间窗应满足 0 <= start_s < end_s <= duration_s")
        if any(start < prior_end and prior_start < end for prior_start, prior_end in intervals[nid]):
            fail(file, field, nid, "同一节点的移动时间窗不能重叠")
        intervals[nid].append((start, end))
        if microseconds(movement.get("duration_s"), file, f"{field}.duration_s", nid) != end - start:
            fail(file, field, nid, "duration_s 必须与 end_s - start_s 一致")
        if movement.get("interpolation") != "linear":
            fail(file, f"{field}.interpolation", nid, "移动插值仅支持 linear")
        distance = math.hypot(event.attrs["toX"] - event.attrs["fromX"], event.attrs["toY"] - event.attrs["fromY"])
        for key, expected in (("distance_m", distance), ("nominal_speed_mps", distance / ((end - start) / 1e6))):
            actual = number(movement.get(key), file, f"{field}.{key}", nid)
            if not math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-6):
                fail(file, f"{field}.{key}", nid, "声明值与起终点和持续时间不一致")
        entries = array(movement.get("samples"), file, f"{field}.samples", nid)
        count = number(movement.get("sample_count"), file, f"{field}.sample_count", nid, integer=True, minimum=1)
        if count != len(entries):
            fail(file, f"{field}.sample_count", nid, "声明的采样数与 samples 数量不一致")
        previous = start
        for j, sample in enumerate(entries):
            sf = f"{field}.samples[{j}]"
            sample = mapping(sample, file, sf, nid)
            time = microseconds(sample.get("time_s"), file, f"{sf}.time_s", nid)
            if not previous < time <= end or (nid, time) in seen:
                fail(file, f"{sf}.time_s", nid, "采样须严格递增、处于移动窗内且不能重复")
            previous = time
            seen.add((nid, time))
            position = _position(sample.get("position"), file, f"{sf}.position", nid)
            fraction = (time - start) / (end - start)
            for axis in ("X", "Y"):
                expected = event.attrs[f"from{axis}"] + fraction * (event.attrs[f"to{axis}"] - event.attrs[f"from{axis}"])
                if not math.isclose(position[axis.lower()], expected, rel_tol=1e-8, abs_tol=1e-4):
                    fail(file, f"{sf}.position.{axis.lower()}", nid, "采样位置与线性轨迹不一致")
            bound = _reference(sample.get("link_event"), set(by_id), file, f"{sf}.link_event", nid)
            if by_id[bound].time_us != time or by_id[bound].event_type != "link_param_change":
                fail(file, f"{sf}.link_event", nid, "采样绑定的链路参数事件必须发生在同一时刻")
            samples[time].append(dict(kind="position_sample", node_id=nid, position=position, bound_event=bound))
        if previous != end:
            fail(file, f"{field}.samples", nid, "最后一个采样点必须覆盖移动终点")
    missing = {event.event_id for event in events if event.event_type == "node_move"} - used_triggers
    if missing:
        fail(file, "movements", ",".join(sorted(missing)),
             "每个 node_move 必须绑定一段 movements 轨迹；仅有起终点不能确定移动持续时间")
    return samples


def _traffic(document, file, services, duration_us):
    document = mapping(document, file, "$")
    if document.get("schema") != "project-traffic-activity-v1":
        fail(file, "schema", "traffic", "活动窗须使用 project-traffic-activity-v1")
    by_id = {s.service_id: s for s in services}
    profiles = mapping(document.get("profiles"), file, "profiles")
    parsed_profiles = {}
    realtime = array(document.get("realtime_types"), file, "realtime_types")
    for kind in realtime:
        string(kind, file, "realtime_types", "traffic")
    for key, profile in profiles.items():
        pf = f"profiles.{key}"
        profile = mapping(profile, file, pf, key)
        ids, seen = [], set()
        for value in array(profile.get("active_service_ids"), file, f"{pf}.active_service_ids", key):
            sid = _unique(value, seen, file, f"{pf}.active_service_ids")
            _reference(sid, set(by_id), file, f"{pf}.active_service_ids", key)
            ids.append(sid)
        total = sum(by_id[sid].required_bps for sid in ids)
        real = sum(by_id[sid].required_bps for sid in ids if by_id[sid].service_type in realtime)
        ratio = real / total if total else 0
        for name, expected in (("base_total_bps", total), ("base_realtime_bps", real), ("base_realtime_ratio", ratio)):
            # 这些是来源文档的派生统计；若保留，就必须与业务清单一致。
            if name in profile:
                value = number(profile[name], file, f"{pf}.{name}", key, integer=name.endswith("bps"))
                if not math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9):
                    fail(file, f"{pf}.{name}", key, "剖面统计与当前业务需求不一致，请同步重新计算")
        if "ratio_range" in profile:
            limits = array(profile["ratio_range"], file, f"{pf}.ratio_range", key)
            if len(limits) != 2:
                fail(file, f"{pf}.ratio_range", key, "实时占比范围应有上下限两个值")
            lo, hi = [number(v, file, f"{pf}.ratio_range", key, maximum=1) for v in limits]
            if not lo <= ratio <= hi:
                fail(file, f"{pf}.ratio_range", key, "配置实时业务占比不在声明范围内")
        parsed_profiles[key] = tuple(ids)
    windows, temporary, boundaries, previous = [], [], {0, duration_us}, 0
    for i, item in enumerate(array(document.get("profile_windows"), file, "profile_windows")):
        field = f"profile_windows[{i}]"
        item = mapping(item, file, field)
        key = _reference(item.get("profile"), set(parsed_profiles), file, f"{field}.profile", field)
        start = microseconds(item.get("start_s"), file, f"{field}.start_s", key)
        end = microseconds(item.get("end_s"), file, f"{field}.end_s", key)
        if start != previous or not start < end <= duration_us:
            fail(file, field, key, "剖面窗口必须连续覆盖全程、无重叠，且 start_s < end_s")
        if any(by_id[sid].registered_at_us > start for sid in parsed_profiles[key]):
            fail(file, field, key, "剖面在业务登记之前启动")
        windows.append((start, end, key))
        boundaries.update((start, end))
        previous = end
    if previous != duration_us:
        fail(file, "profile_windows", "traffic", "剖面窗口必须覆盖至 duration_s")
    by_service = defaultdict(list)
    for i, item in enumerate(array(document.get("temporary_windows", []), file, "temporary_windows")):
        field = f"temporary_windows[{i}]"
        item = mapping(item, file, field)
        sid = _reference(item.get("service_id"), set(by_id), file, f"{field}.service_id", field)
        start = microseconds(item.get("start_s"), file, f"{field}.start_s", sid)
        end = microseconds(item.get("end_s"), file, f"{field}.end_s", sid)
        if not by_id[sid].registered_at_us <= start < end <= duration_us:
            fail(file, field, sid, "临时活动窗必须位于业务登记之后、仿真结束之前，并有正持续时间")
        if any(start < pe and ps < end for ps, pe in by_service[sid]):
            fail(file, field, sid, "同一业务的临时活动窗不能重叠")
        by_service[sid].append((start, end))
        temporary.append((start, end, sid))
        boundaries.update((start, end))
    return parsed_profiles, windows, temporary, boundaries


def _check_official_traffic(traffic, initial, added, duration_us, file):
    """官方输入模式保留全部需求，禁止套用 EXT20 的业务筛选或提前停发。"""
    profiles, windows, temporary, _ = traffic
    expected_ids = {service.service_id for service in initial}
    if any(set(profiles[key]) != expected_ids for _, _, key in windows):
        fail(file, "profile_windows", "official_script", "官方模式每个活动窗口必须包含全部初始业务")
    expected_added = sorted((service.registered_at_us, duration_us, service.service_id)
                            for service in added)
    if sorted(temporary) != expected_added:
        fail(file, "temporary_windows", "official_script", "新增业务必须从官方新增时刻活动至实验结束，不得遗漏、重复或提前停发")


def _compile_batches(events, samples, traffic, services, *, endpoint_at_event=False) -> tuple[EventBatch, ...]:
    profiles, windows, temporary, boundaries = traffic  # 拆出已校验的活动配置。
    by_time = defaultdict(list)  # 同一时刻允许多项核心事件。
    for event in events:
        by_time[event.time_us].append(event)  # append 保留源数组先后顺序。
    # 同刻所有输入归并为一批；不把已有 execution-timeline 作为第二份事件再调度。
    times = sorted(set(by_time) | set(samples) | boundaries)  # 并集去重，再按微秒排序。
    batches, previous_active = [], set()  # 第一批之前尚无活动业务。
    for sequence, time in enumerate(times):
        profile = next((p for start, end, p in windows if start <= time < end), None)  # 左闭右开。
        active = set(profiles[profile]) if profile is not None else set()  # 当前剖面的基础业务。
        active.update(sid for start, end, sid in temporary if start <= time < end)  # 合并临时业务。
        operations = deepcopy(samples.get(time, []))  # 先位置采样。
        for event in by_time.get(time, []):  # 再按原数组顺序应用核心事件。
            operations.append(dict(kind="official_event", event_id=event.event_id, payload=deepcopy(event.raw)))
            if endpoint_at_event and event.event_type == "node_move":
                # 原记录没有移动持续时间及伴随链路参数；仅在同刻回放目标位置。
                # 放在 node_move 之后，避免旧执行器写入起点时覆盖目标；不添加网络扰动。
                attrs = event.attrs
                operations.append(dict(kind="position_sample", node_id=attrs["nodeId"],
                    position={"x": attrs["toX"], "y": attrs["toY"]}, bound_event=event.event_id,
                    semantics="endpoint_at_event"))
        if time in boundaries:  # 只有活动边界需要替换业务发送许可。
            registered = {s.service_id for s in services if s.registered_at_us <= time}  # 不引用未来业务。
            operations.append(dict(kind="traffic_set", application_mode="replace_allowlist",
                                   activate_service_ids=sorted(active - previous_active), stop_new_packets_service_ids=sorted(registered - active),
                                   active_service_ids=sorted(active), queue_rule="do_not_clear_existing_queues"))
        batches.append(EventBatch(time, sequence, tuple(operations), profile, tuple(sorted(active))))
        previous_active = active  # 下一批用它计算新增活动清单。
    return tuple(batches)


def _check_timeline(document, expected, file="execution-timeline.json"):
    document = mapping(document, file, "$")
    if document.get("schema") != "project-execution-batches-v1":
        fail(file, "schema", "timeline", "不支持的批次文件 schema")
    rows = array(document.get("batches"), file, "batches")
    if len(rows) != len(expected):
        fail(file, "batches", "timeline", f"批次数应为 {len(expected)}，实际为 {len(rows)}")
    for i, (row, batch) in enumerate(zip(rows, expected)):
        field = f"batches[{i}]"
        row = mapping(row, file, field)
        obj = f"t={batch.time_s}s"
        time = microseconds(row.get("time_s"), file, f"{field}.time_s", obj)
        us = number(row.get("sim_time_us"), file, f"{field}.sim_time_us", obj, integer=True)
        if us != time or us != batch.time_us:
            fail(file, f"{field}.sim_time_us", obj, "批次时刻、微秒值或排序与源配置不一致")
        if row.get("apply_before_packet_generation") is not True:
            fail(file, f"{field}.apply_before_packet_generation", obj, "批次必须先于该时刻的新分组生成执行")
        for key, value in (("operations", list(batch.operations)), ("profile", batch.profile),
                           ("active_service_ids", list(batch.active_service_ids))):
            # 类型敏感比较：Python 默认会认为 True == 1，此处使用 JSON 编码避免漏检。
            actual = json.dumps(row.get(key), sort_keys=True, ensure_ascii=False)
            wanted = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if actual != wanted:
                # 允许数值的 1 与 1.0 表达差异，但不能允许 bool 替代数值。
                if key != "operations" or row.get(key) != value or _contains_bool_mismatch(row.get(key), value):
                    fail(file, f"{field}.{key}", obj, "批次内容与核心事件、移动采样或活动窗重新编译的结果不一致")


def _contains_bool_mismatch(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return any(_contains_bool_mismatch(left.get(k), v) for k, v in right.items())
    if isinstance(left, list) and isinstance(right, list):
        return any(_contains_bool_mismatch(a, b) for a, b in zip(left, right))
    return isinstance(left, bool) != isinstance(right, bool)


def load_scenario(directory: Path) -> ScenarioSpec:
    """输入场景目录，返回校验后的 ScenarioSpec；错误抛出 ScenarioValidationError。

    读取 scenario-config.json 指定的拓扑、初始业务、事件和活动窗；只有显式
    配置 files.validation_timeline 才核对派生批次。reference_files 仅保存来源。
    新增事件类型时，扩展 _events 的输入校验与 Simulation._apply_event 的执行分支，
    再验证该分支对链路、节点和业务源的影响；批次排序仍由本加载器统一完成。
    """
    reader = _Reader(Path(directory))  # 统一文件边界、原文保留和内容摘要。
    config = mapping(reader.read("scenario-config.json"), "scenario-config.json", "$")  # 先读入口配置。
    allowed = {"scenario_id", "duration_s", "official_dataset", "source_scenario", "files",
               "movements", "expected_final", "reference_files", "input_mode", "movement_semantics"}
    unknown = set(config) - allowed
    if unknown:
        fail("scenario-config.json", "$", "scenario",
             f"未知场景配置字段 {sorted(unknown)}；背景和模型说明请放入 README.md")
    scenario_id = string(config.get("scenario_id"), "scenario-config.json", "scenario_id", "scenario")
    input_mode = config.get("input_mode", "project_activity")
    if input_mode not in ("project_activity", "official_script"):
        fail("scenario-config.json", "input_mode", scenario_id, "仅支持 project_activity 或 official_script")
    official_mode = input_mode == "official_script"
    if official_mode:
        if config.get("movement_semantics") != "endpoint_at_event" or config.get("movements"):
            fail("scenario-config.json", "movement_semantics", scenario_id,
                 "官方输入模式必须明确使用 endpoint_at_event 位置回放，不混入自建连续轨迹")
    elif "movement_semantics" in config:
        fail("scenario-config.json", "movement_semantics", scenario_id, "movement_semantics 仅用于 official_script 模式")
    if "official_dataset" in config and type(config["official_dataset"]) is not bool:
        fail("scenario-config.json", "official_dataset", scenario_id, "数据来源标记必须为布尔值")
    if "source_scenario" in config:
        string(config["source_scenario"], "scenario-config.json", "source_scenario", scenario_id)
    if "expected_final" in config:
        expected = mapping(config["expected_final"], "scenario-config.json", "expected_final", scenario_id)
        fields = {"online_nodes", "available_links", "registered_services", "active_services"}
        if set(expected) != fields:
            fail("scenario-config.json", "expected_final", scenario_id,
                 f"末态审计配置应提供四个计数：{sorted(fields)}")
        for key, value in expected.items():
            number(value, "scenario-config.json", f"expected_final.{key}", scenario_id, integer=True)
    duration_us = microseconds(config.get("duration_s"), "scenario-config.json", "duration_s", scenario_id)  # 内部统一微秒。
    if duration_us <= 0:
        fail("scenario-config.json", "duration_s", scenario_id, "仿真持续时间必须大于零")
    files = mapping(config.get("files"), "scenario-config.json", "files", scenario_id)
    unknown_files = set(files) - {"topology", "initial_services", "events", "traffic", "validation_timeline"}
    if unknown_files:
        fail("scenario-config.json", "files", scenario_id,
             f"未知文件用途 {sorted(unknown_files)}；来源说明文件应列入 reference_files")
    names = {key: string(files.get(key), "scenario-config.json", f"files.{key}", scenario_id)
             for key in ("topology", "initial_services", "events", "traffic")}
    topology = mapping(reader.read(names["topology"]), names["topology"], "$")
    warnings: list[str] = []
    nodes = _nodes(topology, names["topology"])  # 节点身份与地址先独立校验。
    node_ids = {n.node_id for n in nodes}  # 后续端点引用必须出现在此集合。
    links = _links(topology, names["topology"], node_ids, warnings)  # 校验五指标并转换单位。
    service_ids: set[str] = set()
    initial = array(reader.read(names["initial_services"]), names["initial_services"], "$")
    services = tuple(_service(s, names["initial_services"], f"$[{i}]", node_ids, service_ids, warnings)
                     for i, s in enumerate(initial))
    for service in services:
        if service.registered_at_us != 0:
            fail(names["initial_services"], "startAtSec", service.service_id,
                 "初始业务 startAtSec 必须为 0；稍后登记请使用 service_adjust/add 事件，发送时段由活动窗控制")
    events, added = _events(reader.read(names["events"]), names["events"], node_ids,
                            {link.link_id for link in links}, service_ids, warnings, duration_us)
    samples = {} if official_mode else _movement_samples(config, events, node_ids, duration_us)
    traffic = _traffic(reader.read(names["traffic"]), names["traffic"], services + added, duration_us)
    if official_mode:
        _check_official_traffic(traffic, services, added, duration_us, names["traffic"])
        if any(event.event_type == "node_move" for event in events):
            warnings.append("官方移动记录未提供持续时间和伴随链路变化参数；本次按显式 endpoint_at_event 约定回放目标位置，未模拟连续运动或据坐标额外改变链路。")
    batches = _compile_batches(events, samples, traffic, services + added, endpoint_at_event=official_mode)
    if "validation_timeline" in files:
        name = string(files["validation_timeline"], "scenario-config.json", "files.validation_timeline", scenario_id)
        _check_timeline(reader.read(name), batches, file=name)  # 只比较，不再次安排事件。
    # 文件用途显式分类：辅助说明可溯源，但不创建事件、业务或资源分配规则。
    references = array(config.get("reference_files", []), "scenario-config.json", "reference_files", scenario_id)
    seen_references = set()
    for i, name in enumerate(references):
        name = _unique(name, seen_references, "scenario-config.json", f"reference_files[{i}]")
        reader.read(name)
    return ScenarioSpec(scenario_id, duration_us / 1e6, nodes, links, services, added, events, batches,
                        tuple(warnings), dict(reader.hashes), deepcopy(reader.documents))
