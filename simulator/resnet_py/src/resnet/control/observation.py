"""控制端收到的报告：链路观测与业务需求。

这里只保存已经经过网络到达的报告，不持有 Simulation 或场景未来事件。
ObservationCache 按链路两端保存探针；ServiceObservationCache 按业务保存需求。
receive() 是缓存的写入入口；snapshot() 生成独立副本并重新检查新鲜度。
本地真实需求存放在 Simulation.demands，不能直接写入这里。
"""
from __future__ import annotations
from copy import deepcopy
import math

from resnet.contracts.control import (
    OBSERVATION_TTL_NS, require_int, require_text, validate_probe, encode_message,
)
from resnet.contracts.scenario import ServiceSpec

class ObservationCache:
    """按(link_id,reporter_node_id)保存最新接受的探针，两个端点独立计序号。"""

    def __init__(self, link_endpoints: dict[str, tuple[str, str]],
                 max_staleness_ns: int = OBSERVATION_TTL_NS):
        require_int(max_staleness_ns, "max_staleness_ns", 1)
        self.max_staleness_ns = max_staleness_ns
        self.link_endpoints = dict(link_endpoints)
        for lid, pair in self.link_endpoints.items():
            if not isinstance(lid, str) or not lid or len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError("每条静态链路应有唯一ID和两个不同端点")
            if any(not isinstance(nid, str) or not nid for nid in pair):
                raise ValueError("端点ID必须为非空字符串")
        self._reports: dict[tuple[str, str], dict] = {}

    def receive(self, probe: dict, received_at_ns: int, source_node_id: str) -> dict:
        """校验网络来源、采样时效和顺序后保存副本；拒绝时不污染旧缓存。

        source_node_id 由网络接收回调提供，不使用 probe 自报字段作为来源凭据。
        返回accepted/reason与时刻，便于日志区分网络到达和通过新鲜度检查。
        """
        require_int(received_at_ns, "received_at_ns")
        try:
            validate_probe(probe)
        except (ValueError, TypeError) as error:
            return {"accepted": False, "reason": "malformed_probe", "detail": str(error),
                    "received_at_ns": received_at_ns}
        lid, nid = probe["link"]["link_id"], probe["node_id"]
        result = {"accepted": False, "reason": None, "node_id": nid, "link_id": lid,
                  "seq": probe["seq"], "observed_at_ns": probe["observed_at_ns"],
                  "received_at_ns": received_at_ns,
                  "observation_delay_ns": received_at_ns - probe["observed_at_ns"]}
        endpoints = self.link_endpoints.get(lid)
        if nid != source_node_id:
            result["reason"] = "source_identity_mismatch"
        elif endpoints is None or nid not in endpoints:
            result["reason"] = "nonlocal_link_report"
        elif probe["link"]["peer_node_id"] != endpoints[1 - endpoints.index(nid)]:
            result["reason"] = "peer_identity_mismatch"
        elif received_at_ns < probe["observed_at_ns"]:
            result["reason"] = "future_observation"
        elif received_at_ns - probe["observed_at_ns"] > self.max_staleness_ns:
            result["reason"] = "stale_observation"
        else:
            previous = self._reports.get((lid, nid))
            if previous and probe["seq"] <= previous["probe"]["seq"]:
                result["reason"] = "nonincreasing_sequence"
            elif previous and probe["observed_at_ns"] < previous["probe"]["observed_at_ns"]:
                result["reason"] = "older_sample_time"
            else:
                # 不持有调用者的可变字典，避免后续本地更新悄悄改写历史报告。
                self._reports[(lid, nid)] = {"probe": deepcopy(probe), "received_at_ns": received_at_ns}
                result.update(accepted=True, reason="accepted")
        return result

    def snapshot(self, now_ns: int) -> dict:
        """重新检查有效期，生成策略可读的保守链路视图。

        到达时新鲜不等于决策时仍新鲜。容量取双端较小值，时延/抖动/损伤取
        较大值；双端均报告在线和up且都在10秒内时，usable才为True。
        旧记录保留供诊断，但失效边不进入策略图；这里不推断全局真实在线状态。
        """
        require_int(now_ns, "now_ns")
        links, versions = {}, {}
        for lid, (src, dst) in sorted(self.link_endpoints.items()):
            reports = {nid: self._reports.get((lid, nid)) for nid in (src, dst)}
            row = {"link_id": lid, "src_node_id": src, "dst_node_id": dst,
                   "usable": False, "reason": None, "reporters": deepcopy(reports)}
            present = [r for r in reports.values() if r is not None]
            for report in present:
                p = report["probe"]
                if report["received_at_ns"] <= now_ns and 0 <= now_ns - p["observed_at_ns"] <= self.max_staleness_ns:
                    versions[p["node_id"]] = max(versions.get(p["node_id"], 0), p["version"])
            if len(present) != 2:
                row["reason"] = "missing_endpoint_observation"
            elif any(r["received_at_ns"] > now_ns or r["probe"]["observed_at_ns"] > now_ns for r in present):
                row["reason"] = "future_snapshot_time"
            elif any(now_ns - r["probe"]["observed_at_ns"] > self.max_staleness_ns for r in present):
                row["reason"] = "stale_endpoint_observation"
            else:
                p1, p2 = [r["probe"] for r in present]
                q1, q2 = p1["link"], p2["link"]
                row.update(capacity_bps=min(q1["capacity_bps"], q2["capacity_bps"]),
                           delay_ns=max(q1["delay_ns"], q2["delay_ns"]),
                           jitter_ns=max(q1["jitter_ns"], q2["jitter_ns"]),
                           packet_loss_rate=max(q1["packet_loss_rate"], q2["packet_loss_rate"]),
                           ber=max(q1["ber"], q2["ber"]),
                           queue_packets=max(q1["queue_packets"], q2["queue_packets"]),
                           observed_at_ns=min(p1["observed_at_ns"], p2["observed_at_ns"]))
                if not (p1["node_online"] and p2["node_online"] and q1["endpoint_online"] and q2["endpoint_online"]):
                    row["reason"] = "endpoint_offline"
                elif not (q1["admin_up"] and q2["admin_up"]):
                    row["reason"] = "link_down"
                else:
                    row.update(usable=True, reason="fresh_both_endpoints_up")
            links[lid] = row
        return {"sampled_for_decision_at_ns": now_ns, "max_staleness_ns": self.max_staleness_ns,
                "links": links, "node_versions": versions,
                "usable_link_count": sum(r["usable"] for r in links.values()),
                "received_report_count": len(self._reports)}


SERVICE_FIELDS = {'service_id','service_type','category','src_node_id','dst_node_id',
                  'priority','required_bps','delay_limit_s','jitter_limit_s','ber_limit','loss_limit'}


def describe_service(service):
    """本地源节点把已登记业务转成单包定义；不把raw及未来登记时刻发给控制器。"""
    return {name: getattr(service, name) for name in sorted(SERVICE_FIELDS)}


def validate_service_status(message):
    """严格检查结构、单位、有限数与单包长度；返回原字典。"""
    fields = {'kind','node_id','seq','observed_at_ns','revision','registered','active','service'}
    if not isinstance(message, dict) or set(message) != fields or message['kind'] != 'service_status':
        raise ValueError('service_status字段不符')
    require_text(message['node_id'],'node_id')
    for key in ('seq','observed_at_ns','revision'):
        require_int(message[key],key)
    for key in ('registered','active'):
        if type(message[key]) is not bool:
            raise ValueError(key+'必须为bool')
    if message['active'] and not message['registered']:
        raise ValueError('未登记业务不能活动')
    service = message['service']
    if not isinstance(service,dict) or set(service) != SERVICE_FIELDS:
        raise ValueError('业务定义字段不符')
    for key in ('service_id','service_type','src_node_id','dst_node_id'):
        require_text(service[key],key)
    if service['category'] is not None and not isinstance(service['category'],str):
        raise ValueError('category必须为str或None')
    require_int(service['priority'],'priority')
    require_int(service['required_bps'],'required_bps',1)
    for key in ('delay_limit_s','jitter_limit_s','ber_limit','loss_limit'):
        val = service[key]
        if type(val) not in (int,float) or not math.isfinite(val) or val < 0:
            raise ValueError('业务门限应为非负有限数')
        if key in ('ber_limit','loss_limit') and val > 1:
            raise ValueError('概率门限应不大于1')
    if service['src_node_id'] == service['dst_node_id']:
        raise ValueError('不支持同节点业务')
    encode_message(message)  # 完整JSON必须能放入一个实际UDP包。
    return message


class ServiceObservationCache:
    """缓存已到达的业务状态；revision代表需求变更，seq代表重发/周期采样。"""
    def __init__(self, node_ids, max_staleness_ns=10_000_000_000):
        self.node_ids = set(node_ids)
        self.max_staleness_ns = require_int(max_staleness_ns,'max_staleness_ns',1)
        self.records = {}

    def receive(self, message, received_at_ns, source_node_id):
        require_int(received_at_ns,'received_at_ns')
        try:
            validate_service_status(message)
        except (ValueError,TypeError):
            return {'accepted':False,'reason':'malformed_service_status'}
        service = message['service']; sid = service['service_id']
        if (source_node_id != message['node_id'] or source_node_id != service['src_node_id']
                or source_node_id not in self.node_ids or service['dst_node_id'] not in self.node_ids):
            return {'accepted':False,'reason':'service_source_mismatch'}
        elapsed = received_at_ns - message['observed_at_ns']
        if elapsed < 0 or elapsed > self.max_staleness_ns:
            return {'accepted':False,'reason':'future_service_status' if elapsed < 0 else 'stale_service_status'}
        old = self.records.get(sid)
        if old:
            if old['service'] != service:
                return {'accepted':False,'reason':'service_definition_conflict'}
            if message['seq'] <= old['seq'] or message['observed_at_ns'] < old['observed_at_ns']:
                return {'accepted':False,'reason':'old_service_sequence'}
            if message['revision'] < old['revision']:
                return {'accepted':False,'reason':'old_demand_revision'}
            if message['revision'] == old['revision'] and any(message[k] != old[k] for k in ('registered','active')):
                return {'accepted':False,'reason':'revision_state_conflict'}
        self.records[sid] = deepcopy(message) | {'received_at_ns':received_at_ns}
        return {'accepted':True,'reason':'accepted'}

    def snapshot(self, now_ns):
        require_int(now_ns,'now_ns')
        result = {}
        for sid, record in sorted(self.records.items()):
            elapsed = now_ns - record['observed_at_ns']
            fresh = 0 <= elapsed <= self.max_staleness_ns
            result[sid] = deepcopy(record) | {'fresh':fresh,
                'reason':'fresh' if fresh else 'stale_service_status'}
        return {'sampled_for_decision_at_ns':now_ns,'services':result}

    def get_spec(self, service_id):
        """仅由已收到的描述还原业务。

        ServiceSpec要求登记时间字段，此处填当前报告观测时刻作内部占位，
        不声称知道实际登记时间；路径规则不使用该字段，也无未来时间表。
        """
        row = self.records[service_id]
        return ServiceSpec(**row['service'],registered_at_us=row['observed_at_ns']//1000,raw={})
