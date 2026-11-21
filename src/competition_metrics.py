"""Competition metrics calculated after the episode from delivered evidence and device receipts."""
from collections import defaultdict
import json
from math import ceil, isclose
from pathlib import Path


def records(path, *, optional=False):
    # The native engine opens a device log on its first execution.
    if optional and not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0, "meanNs": None, "p95Ns": None, "maxNs": None}
    if values[0] < 0:
        raise ValueError("Competition clock has a negative interval")
    return {"n": len(values), "meanNs": sum(values) / len(values),
            "p95Ns": values[ceil(.95 * len(values)) - 1], "maxNs": values[-1]}


def entity(event):
    attrs = event["attrs"]
    return attrs.get("linkId") or attrs.get("serviceId") or attrs.get("nodeId")


def requested_fields_match(attrs, actual, fields):
    checked = 0
    for key, (field, scale) in fields.items():
        if key in attrs:
            if field not in actual or not isclose(actual[field], attrs[key] * scale, rel_tol=1e-9, abs_tol=1e-12):
                return False
            checked += 1
    return checked > 0


LINK_FIELDS = {"delayMs": ("delay_ns", 1e6), "jitterMs": ("jitter_ns", 1e6),
               "bandwidthBps": ("capacity_bps", 1), "ber": ("ber", 1),
               "packetLossRate": ("packet_loss_rate", 1)}
SERVICE_FIELDS = {"delayMs": ("delay_limit_s", .001), "jitterMs": ("jitter_limit_s", .001),
                  "bandwidthBps": ("required_bps", 1), "ber": ("ber_limit", 1),
                  "packetLossRate": ("loss_limit", 1)}


def service_matches(attrs, message):
    spec = message["service"]
    if attrs.get("op") in ("delete", "remove", "del"):
        return not message["active"]
    if not (message["registered"] and message["active"]):
        return False
    for field, key in (("priority", "priority"), ("serviceType", "service_type"),
                       ("srcNodeId", "src_node_id"), ("dstNodeId", "dst_node_id")):
        if field in attrs and attrs[field] != spec.get(key):
            return False
    req = attrs.get("requirement", {})
    return requested_fields_match(req, spec, SERVICE_FIELDS) if req else (
        attrs.get("op") == "add" or any(k in attrs for k in ("priority", "serviceType")))


def event_windows(directory, conditions, start, end, stop):
    script = conditions["script"]
    obj = json.loads((directory / "input" / "scripts" / f"script-{script}.json").read_bytes())
    events = obj if isinstance(obj, list) else obj["events"]
    windows = defaultdict(list)
    next_change = {}
    for index, event in reversed(list(enumerate(events))):
        stamp = round(event["time"] * 1e9)
        target = entity(event)
        limit = next_change.get(target, stop + 1)
        next_change[target] = stamp
        if start <= stamp < end:
            windows[target].append({"index": index, "type": event["eventType"], "entity": target,
                                    "eventNs": stamp, "cutoffNs": min(limit, stop + 1),
                                    "evidenceArrivedNs": None, "samples": {}, "attrs": event["attrs"]})
    return windows


def accept_report(windows, row, ttl):
    if not row.get("result", {}).get("accepted"):
        return
    arrival = row["time_ns"]
    if row["event"] == "observation":
        report = row["probe"]
        keys = (report["link"]["link_id"], report["node_id"])
    elif row["event"] == "service_observation":
        report = row["message"]
        keys = (report["service"]["service_id"],)
    else:
        return
    sampled = report["observed_at_ns"]
    if not 0 <= arrival - sampled <= ttl:
        return
    for key in keys:
        for rec in windows.get(key, ()):
            if rec["evidenceArrivedNs"] is not None or not rec["eventNs"] <= sampled <= arrival < rec["cutoffNs"]:
                continue
            kind, attrs = rec["type"], rec["attrs"]
            if kind in ("link_param_change", "link_state") and row["event"] == "observation":
                link = report["link"]
                match = (requested_fields_match(attrs, link, LINK_FIELDS) if kind == "link_param_change"
                         else link["admin_up"] == (attrs["state"] == "up"))
                node = report["node_id"]
                if match:
                    rec["samples"][node] = (sampled, arrival)
                else:
                    rec["samples"].pop(node, None)
                # RC requires fresh evidence from both ends of this link.
                peer = rec["samples"].get(link["peer_node_id"])
                if match and peer and arrival - peer[0] <= ttl:
                    rec["evidenceArrivedNs"] = arrival
            elif ((kind in ("node_online", "node_offline") and row["event"] == "observation"
                   and report["node_id"] == key and report["node_online"] == (kind == "node_online"))
                  or (kind == "service_adjust" and row["event"] == "service_observation"
                      and service_matches(attrs, report))):
                rec["samples"][report["node_id"]] = (sampled, arrival)
                rec["evidenceArrivedNs"] = arrival


def sensed_events(windows, end):
    values = []
    for group in windows.values():
        for rec in group:
            arrived = rec["evidenceArrivedNs"]
            status = "observed" if arrived is not None else "no_matching_delivered_evidence"
            if rec["type"] == "node_move":
                status = "coordinates_absent_from_control_reports"
            values.append({"index": rec["index"], "type": rec["type"], "entity": rec["entity"],
                           "eventNs": rec["eventNs"], "evidenceArrivedNs": arrived,
                           "delayNs": None if arrived is None else arrived - rec["eventNs"],
                           "duringDrain": bool(arrived is not None and arrived >= end),
                           "status": status, "evidence": rec["samples"] if arrived is not None else {}})
    # JSON arrays in sealed receipts and in-memory values have the same representation.
    return sorted(json.loads(json.dumps(values)), key=lambda item: item["index"])


def latest_input_arrival(state):
    times = [r["received_at_ns"] for link in state["links"].values() for r in link["reports"].values()
             if r and r.get("status") == "fresh"]
    times.extend(r["received_at_ns"] for r in state["services"].values() if r.get("fresh"))
    return max(times, default=None)


def competition_metrics(directory, summary):
    directory = Path(directory)
    cond = json.loads((directory / "CONDITIONS.json").read_bytes())
    clock = summary["clock"]
    windows = event_windows(directory, cond, clock["start_ns"], clock["end_ns"], clock["drain_end_ns"])
    ttl = cond["simulation"]["control"]["observation_ttl_ns"]
    writes = {}
    for row in records(directory / "controller.jsonl"):
        accept_report(windows, row, ttl)
        if row["event"] == "command_sent":
            c = row["command"]
            if c["action"]["op"] != "query":
                writes.setdefault(c["command_id"], (c["plan_id"].split("/")[0], row["time_ns"]))
    applied = defaultdict(list)
    deliveries = defaultdict(dict)
    for row in records(directory / "executions.jsonl", optional=True):
        cid = row["command"]["command_id"]
        if cid in writes:
            wid, sent = writes[cid]
            received = row["received_at_ns"]
            if not sent <= received <= row["time_ns"]:
                raise ValueError("Device receipt predates command issue")
            deliveries[wid].setdefault(cid, received)
            if row["status"] == "applied":
                applied[wid].append(row["time_ns"])
    evidence = {}
    clocks = {}
    orders = {}
    endings = {}
    for row in records(directory / "b03.jsonl"):
        kind = row["event"]
        wid = row.get("work_id")
        if kind == "b03_plan_evidence_clock":
            key = (wid, row["snapshot_id"])
            stamp = row["fresh_arrival_ns"]
            if key in clocks or (stamp is not None and (type(stamp) is not int or not 0 <= stamp <= row["time_ns"])):
                raise ValueError("Invalid or duplicate bound observation clock")
            clocks[key] = stamp
        elif kind == "b03_observation_evidence" and row.get("snapshot_id"):
            evidence[(wid, row["snapshot_id"])] = latest_input_arrival(row["state"])
        elif kind == "b03_rc_plan":
            now = row["time_ns"]
            wall = row["decision_wall_ns"]
            if type(wall) is not int or wall < 0:
                raise ValueError("Invalid resource controller duration")
            times = row.get("component_timing", {})
            if any(type(v) is not int or v < 0 for v in times.values()):
                raise ValueError("Invalid component timing")
            if times.get("chargedComponentWallNs") is not None:
                if wall != row["solver_wall_ns"] + row["tool_choice"]["selectionWallNs"] + times["chargedComponentWallNs"]:
                    raise ValueError("Recorded component and policy durations disagree")
            orders[wid] = {"id": wid, "snapshot": row["snapshot_id"], "decisionNs": now,
                           "planReadyNs": now + wall, "generationComputeNs": wall,
                           "requestedServices": len(set(row["release_ids"] + row["install_ids"])),
                           "outcome": "no_device_change" if not row["release_ids"] and not row["install_ids"] else "pending"}
            if clocks:
                status = (row.get("solver_audit") or {}).get("solverStatus")
                orders[wid]["solverOutcome"] = status
                orders[wid]["fallbackHold"] = status == "proposal_rejected_fallback_hold"
        elif wid and kind in ("work_verified", "work_incomplete", "work_rejected"):
            endings[wid] = (kind, row["time_ns"])
    issued = defaultdict(list)
    for wid, stamp in writes.values():
        issued[wid].append(stamp)
    for wid, order in orders.items():
        ready = order["planReadyNs"]
        first = min(issued[wid], default=None)
        key = (wid, order["snapshot"])
        if clocks:
            if key not in clocks or (key in evidence and clocks[key] != evidence[key]):
                raise ValueError("Observation timestamp disagrees with the bound plan input")
            seen = clocks[key]
            order["evidenceStatus"] = "delivered" if seen is not None else "no_fresh_evidence"
        else:
            seen = evidence.get(key)
        order["evidenceArrivedNs"] = seen
        order["evidenceToPlanReadyNs"] = ready - seen if seen is not None else None
        order["firstWriteIssuedNs"] = first
        order["lastWriteReceivedNs"] = max(deliveries[wid].values(), default=None)
        order["lastDeviceAppliedNs"] = max(applied[wid], default=None)
        order["writeCount"] = len(issued[wid])
        order["deliveredWriteCount"] = len(deliveries[wid])
        order["readyToFirstIssueNs"] = first - ready if first is not None else None
        all_received = bool(first is not None and len(deliveries[wid]) == len(issued[wid]))
        outcome, closed = endings.get(wid, (order["outcome"], None))
        order["outcome"] = outcome
        order["completeDispatchNs"] = (order["lastWriteReceivedNs"] - first
                                       if all_received and outcome == "work_verified" else None)
        order["signedReadbackNs"] = closed if outcome == "work_verified" else None
    work = list(orders.values())
    events = sensed_events(windows, clock["end_ns"])
    end = clock["end_ns"]
    scored = [row for row in work if clock["start_ns"] <= row["decisionNs"] < end]
    business = summary["business"]
    result = {"schema": "b03.competition.v1", "clock": "simulation_ns", "events": events, "workOrders": work,
            "perception": distribution(x["delayNs"] for x in events if not x["duringDrain"]),
            "generation": distribution(x["evidenceToPlanReadyNs"] for x in scored if x["planReadyNs"] < end),
            "dispatch": distribution(x["completeDispatchNs"] for x in scored
                                     if x["signedReadbackNs"] is not None and x["signedReadbackNs"] < end),
            "includingDrain": {"perception": distribution(x["delayNs"] for x in events),
                               "generation": distribution(x["evidenceToPlanReadyNs"] for x in work),
                               "dispatch": distribution(x["completeDispatchNs"] for x in work)},
            "coverage": {"inWindowEvents": len(events), "plans": len(scored), "drainOnlyPlans": len(work)-len(scored),
                         "configurationPlans": sum(bool(x["requestedServices"]) for x in scored),
                         "verifiedBeforeEnd": sum(x["signedReadbackNs"] is not None and x["signedReadbackNs"] < end
                                                  for x in scored),
                         "verifiedThroughDrain": sum(x["outcome"] == "work_verified" for x in work)},
            "connectivity": {"timeMeanOverOnlineNodePairs": business["onlinePairConnectedTimeMean"],
                             "timeMeanOverActiveEndpoints": business["activeEndpointReachableTimeMean"]},
            "serviceGuarantee": {"requestedServiceSeconds": business["requestSeconds"],
                "unservedServiceSeconds": business["U"], "priorityWeightedUnservedSeconds": business["W"],
                "servedFraction": None if business["UOverRequests"] is None else 1 - business["UOverRequests"]}}
    if clocks:
        result["schema"] = "b03.competition.v2"
        result["coverage"]["fallbackHolds"] = sum(x.get("fallbackHold", False) for x in scored)
        result["coverage"]["plansWithoutFreshEvidence"] = sum(x["evidenceStatus"] == "no_fresh_evidence" for x in scored)
        result["coverage"]["plansReadyAtOrAfterEnd"] = sum(x["planReadyNs"] >= end for x in scored)
    return result
