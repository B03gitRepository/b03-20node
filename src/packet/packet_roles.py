"""B03 rules for the new packet simulator. All inputs are delivered controller caches."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
from functools import lru_cache
import inspect
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

from packet_a2a import PacketA2A, digest
from resnet.contracts.control import OBSERVATION_TTL_NS
from resnet.control.observation import SERVICE_FIELDS, describe_service
from resnet.control.policy import _quality, required_wire_bps

_DEFAULT_RUNTIME = Path(__file__).resolve().parent.parent / "rules"
RUNTIME = Path(os.environ.get("B03_PACKET_RUNTIME", _DEFAULT_RUNTIME)).resolve()
if not (RUNTIME / "vendor" / "src" / "b03" / "solver.py").is_file():
    raise ImportError(f"B03_PACKET_RUNTIME does not contain the T119 solver: {RUNTIME}")
sys.path[:0] = [str(RUNTIME / "vendor" / "src"), str(RUNTIME)]
from b03.solver import assess, loads, solve
from official20_adapter import candidate_scope
from strong_tools import default_authority, enumerate_actions, solve_action, validate_proposal

CONFIG = {"path_limit": 4, "path_expansion_budget": 2000, "search_budget": 2000,
          "search_service_limit": 12, "max_path_hops": None}
METRICS = ("capacity_bps", "delay_ns", "jitter_ns", "ber", "packet_loss_rate")
CHOICE_FIELDS = ("service_id", "accepted", "node_path", "link_path",
                 "reserved_bps", "wire_bps", "demand_revision")
DOWN = {"link_down", "endpoint_offline"}


@lru_cache(maxsize=1)
def _implementations():
    files = {}
    for fn in (solve, default_authority, solve_action, validate_proposal, candidate_scope):
        path = Path(inspect.getfile(fn)).resolve()
        files[fn.__module__ + "." + fn.__name__] = {
            "file": path.relative_to(RUNTIME).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return files


def _packet_options(payload_bytes, epoch_overhead_bytes, control_headroom_fraction):
    if (payload_bytes, epoch_overhead_bytes) != (1000, 8):
        raise ValueError("Packet control mode requires 1000 payload bytes and 8 epoch bytes")
    if (type(control_headroom_fraction) not in (int, float)
            or not 0 <= control_headroom_fraction < 1):
        raise ValueError("Invalid fractional control headroom")
    return {"payload_bytes": payload_bytes, "epoch_overhead_bytes": epoch_overhead_bytes,
            "control_headroom_fraction": control_headroom_fraction}


def _delivered_probe(report, *, lid, nid, peer, now, ttl):
    if report is None:
        return None
    p, arrived = report["probe"], report["received_at_ns"]
    sampled = p["observed_at_ns"]
    if not (type(sampled) is int and type(arrived) is int and 0 <= sampled <= arrived <= now):
        return None
    q = p["link"]
    if p["node_id"] != nid or q["link_id"] != lid or q["peer_node_id"] != peer:
        raise ValueError("Endpoint identity differs from the delivered probe")
    return {"status": "fresh" if now - sampled <= ttl else "stale",
            "sampled_at_ns": sampled, "received_at_ns": arrived, "seq": p["seq"], "version": p["version"],
            "node_online": p["node_online"], "admin_up": q["admin_up"], "endpoint_online": q["endpoint_online"],
            **{k: q[k] for k in METRICS}}


def _project_links(now_ns, snapshot):
    if snapshot.get("sampled_for_decision_at_ns") != now_ns:
        raise ValueError("Expected a link-cache snapshot sampled at this decision time")
    ttl = snapshot.get("max_staleness_ns", OBSERVATION_TTL_NS)
    out = {}
    for lid, row in snapshot["links"].items():
        a, b = row["src_node_id"], row["dst_node_id"]
        reports = {nid: _delivered_probe(row.get("reporters", {}).get(nid), lid=lid, nid=nid,
                                         peer=peer, now=now_ns, ttl=ttl) for nid, peer in ((a, b), (b, a))}
        link = {"src_node_id": a, "dst_node_id": b, "reports": reports, "usable": False}
        if any(v is None for v in reports.values()) or row["reason"] == "future_snapshot_time":
            link["reason"] = "missing_endpoint_observation"
        elif any(v["status"] == "stale" for v in reports.values()):
            link["reason"] = "stale_endpoint_observation"
        else:
            # ObservationCache.snapshot owns the physical aggregation. Its row can
            # reach a role only after both underlying reports have arrived and are fresh.
            link.update({k: row[k] for k in ("usable", "reason", "observed_at_ns", *METRICS) if k in row})
        out[lid] = link
    return out


def _signature(decision):
    return {k: deepcopy(decision[k]) for k in CHOICE_FIELDS if k in decision}


def _state(now_ns, links_snapshot, demands_snapshot, confirmed, retained):
    if demands_snapshot.get("sampled_for_decision_at_ns") != now_ns:
        raise ValueError("Expected a demand-cache snapshot sampled at this decision time")
    services = {}
    for sid, row in demands_snapshot["services"].items():
        if not (0 <= row["observed_at_ns"] <= row["received_at_ns"] <= now_ns):
            continue
        spec = row["service"]
        if sid != spec["service_id"] or row["node_id"] != spec["src_node_id"]:
            raise ValueError("Demand and reporting source identities differ")
        services[sid] = {k: row[k] for k in ("node_id", "seq", "fresh", "registered", "active", "revision",
                                             "observed_at_ns", "received_at_ns")}
        services[sid]["service"] = {k: spec[k] for k in sorted(SERVICE_FIELDS)}
    links = _project_links(now_ns, links_snapshot)
    ttl = links_snapshot.get("max_staleness_ns", OBSERVATION_TTL_NS)
    latest_probes = {}
    for link in links.values():
        for nid, probe in link["reports"].items():
            if probe is None or probe["status"] != "fresh":
                continue
            prev = latest_probes.get(nid)
            if prev is None or (probe["version"], probe["sampled_at_ns"]) > (prev["version"], prev["sampled_at_ns"]):
                latest_probes[nid] = probe
    floors = dict(links_snapshot.get("controller_known_node_versions", {}))
    if any(type(v) is not int or v < 0 for v in floors.values()):
        raise ValueError("Invalid delivered node version floor")
    acks = {}
    for sid, record in confirmed.items():
        if not 0 <= record["confirmed_at_ns"] <= now_ns:
            continue
        d = _signature(record["decision"])
        if sid != d["service_id"] or d["accepted"] is not True:
            raise ValueError("Invalid confirmed service")
        samples = {}
        for nid, rec in record.get("readback_nodes", {}).items():
            sampled, arrived = rec.get("observed_at_ns"), rec.get("arrived_at_ns")
            if (nid in d["node_path"] and type(sampled) is int and type(arrived) is int
                    and 0 <= sampled <= arrived <= now_ns):
                samples[nid] = {k: deepcopy(rec[k]) for k in
                                ("phase", "work_id", "service_id", "epoch", "node_id", "network_source",
                                 "query_id", "version", "issued_at_ns", "observed_at_ns", "arrived_at_ns",
                                 "result", "matched") if k in rec}
        witness = _configuration_evidence(d, record["epoch"], samples, services.get(sid),
                                          latest_probes, floors, now_ns, ttl)
        acks[sid] = {"epoch": record["epoch"], "confirmed_at_ns": record["confirmed_at_ns"],
                     "decision": d, "readback_nodes": samples, "configuration_evidence": witness}
    return {"now_ns": now_ns, "links": links,
            "node_versions": {nid: p["version"] for nid, p in latest_probes.items()},
            "link_ttl_ns": ttl, "controller_known_node_versions": floors,
            "services": services, "confirmed": acks, "retained_wire_bps": dict(retained or {})}


def _request(service):
    return {"serviceId": service["service_id"], "srcNodeId": service["src_node_id"],
            "dstNodeId": service["dst_node_id"], "priority": service["priority"],
            "category": service["category"], "serviceType": service["service_type"],
            "requirement": {"bandwidthBps": required_wire_bps(service["required_bps"]),
                            "delayMs": service["delay_limit_s"] * 1000,
                            "jitterMs": service["jitter_limit_s"] * 1000, "ber": service["ber_limit"],
                            "packetLossRate": service["loss_limit"]},
            "requiredPayloadBps": service["required_bps"]}


def _assignment(choice):
    return {"nodes": list(choice["node_path"]), "linkIds": list(choice["link_path"]),
            "allocatedBps": choice["wire_bps"]}


def _graph(raw_links, headroom, fixed):
    if set(fixed) - set(raw_links) or any(type(value) is not int or value < 0 for value in fixed.values()):
        raise ValueError("Invalid link or value in retained wire budget")
    nodes, links, budget = {}, {}, {}
    fraction = Decimal(1) - Decimal(str(headroom))
    for lid, row in raw_links.items():
        src, dst = row["src_node_id"], row["dst_node_id"]
        up = row["usable"]
        nodes.setdefault(src, {"_online": False})
        nodes.setdefault(dst, {"_online": False})
        if up:
            nodes[src]["_online"] = nodes[dst]["_online"] = True
        budget[lid] = int(Decimal(row["capacity_bps"]) * fraction) if up else 0
        links[lid] = {"srcNodeId": src, "dstNodeId": dst, "_online": up,
                      "params": {"bandwidthBps": max(0, budget[lid] - fixed.get(lid, 0)),
                                 "delayMs": row.get("delay_ns", 0) / 1_000_000,
                                 "jitterMs": row.get("jitter_ns", 0) / 1_000_000,
                                 "packetLossRate": row.get("packet_loss_rate", 0),
                                 "ber": row.get("ber", 0)}}
    return nodes, links, budget


def _choice(service, path, raw_links, *, revision=None, reason="no_feasible_b03_candidate"):
    wire = required_wire_bps(service["required_bps"])
    choice = {"service_id": service["service_id"], "priority": service["priority"],
              "accepted": False, "reason": reason, "node_path": [], "link_path": [],
              "required_payload_bps": service["required_bps"], "required_wire_bps": wire,
              "reserved_bps": 0, "wire_bps": 0, "quality_estimates": None, "violations": []}
    if revision is not None:
        choice.update(revision=revision, demand_revision=revision)
    if path is None:
        return choice
    if path["allocatedBps"] != wire:
        raise ValueError("B03 modified the original payload demand")
    if any(not raw_links[lid]["usable"] for lid in path["linkIds"]):
        choice["reason"] = "hold_existing_unusable_path"
        return choice
    quality, violations = _quality(SimpleNamespace(**service), path["linkIds"], raw_links, 1036,
                                   qos_profile="official")
    choice.update(quality_estimates=quality, violations=violations)
    if violations:
        choice["reason"] = "selected_candidate_failed_official_qos"
        return choice
    choice.update(accepted=True, reason="accepted", node_path=list(path["nodes"]),
                  link_path=list(path["linkIds"]), reserved_bps=service["required_bps"], wire_bps=wire)
    return choice


def single_policy(snapshot, services, *, now_ns, payload_bytes=1000, epoch_overhead_bytes=8,
                  control_headroom_fraction=.05, retained_wire_bps=None):
    """Native DecisionPolicy, using the original B03 priority_greedy implementation."""
    options = _packet_options(payload_bytes, epoch_overhead_bytes, control_headroom_fraction)
    raw = _project_links(now_ns, snapshot)
    retained = dict(retained_wire_bps or {})
    nodes, links, budget = _graph(raw, control_headroom_fraction, retained)
    specs = [describe_service(s) for s in services]
    requests = {s["service_id"]: _request(s) for s in specs}
    if len(requests) != len(specs):
        raise ValueError("Duplicate service in DecisionPolicy input")
    started = time.perf_counter_ns()
    target, audit = solve(nodes, links, requests, {}, now_ns / 1e9,
                          dict(CONFIG, protected_service_ids=[]), "priority_greedy")
    elapsed = time.perf_counter_ns() - started
    remaining = {lid: max(0, cap - retained.get(lid, 0)) for lid, cap in budget.items()}
    decisions = []
    for s in sorted(specs, key=lambda row: (-row["priority"], row["service_id"])):
        d = _choice(s, target[s["service_id"]], raw)
        if d["accepted"]:
            for lid in d["link_path"]:
                remaining[lid] -= d["wire_bps"]
        decisions.append(d)
    return {"decided_at_ns": now_ns, "policy": "b03.solver.solve/priority_greedy",
            "decisions": decisions, "solver_wall_ns": elapsed, "solver_audit": audit,
            "initial_wire_budget_bps": budget, "retained_wire_bps": retained,
            "remaining_wire_bps": remaining, **options}


def _superseded(demand, ack):
    return (ack is not None and demand["fresh"] and demand["active"] and demand["registered"]
            and demand["revision"] > ack["decision"]["demand_revision"])


def _model(state, headroom, permitted):
    # Model costs and assignments use wire bps. Confirmed rows with unknown demand,
    # old epochs, and uncertain preparations keep consuming the unmodelled balance.
    raw, acks = state["links"], state["confirmed"]
    demands = {sid: row for sid, row in state["services"].items() if row["fresh"]}
    requests = {sid: dict(_request(row["service"]), _deleted=not (row["active"] and row["registered"]))
                for sid, row in demands.items()}
    superseded = {sid for sid, row in demands.items() if _superseded(row, acks.get(sid))}
    uncertain = {sid for sid, record in acks.items() if not record["configuration_evidence"]["verified"]}
    # The native source withdraws its permit on every demand revision. An older
    # epoch is still on the wire ledger until the device confirms its retirement.
    current = {sid: _assignment(acks[sid]["decision"]) if sid in acks and sid not in superseded and sid not in uncertain else None
               for sid in requests}
    modeled_load = loads(raw, current)
    conditional = loads(raw, {sid: _assignment(acks[sid]["decision"]) for sid in (superseded & permitted) - uncertain})
    confirmed_load = Counter()
    for item in acks.values():
        for lid in item["decision"]["link_path"]:
            if lid not in raw:
                raise ValueError("Confirmed assignment references an unknown link")
            confirmed_load[lid] += item["decision"]["wire_bps"]
    reported = state["retained_wire_bps"]
    if set(reported) - set(raw) or any(type(v) is not int or v < 0 for v in reported.values()):
        raise ValueError("Invalid controller wire ledger")
    fixed = {lid: max(reported.get(lid, 0), confirmed_load[lid]) - modeled_load[lid] - conditional[lid]
             for lid in raw}
    nodes, links, budget = _graph(raw, headroom, fixed)
    snapshot = {"nodes": nodes, "links": links, "requests": requests, "assignments": current,
                "observedAtSec": state["now_ns"] / 1e9, "stateVersion": digest(state),
                "configVersion": digest({"acks": acks, "node_versions": state["node_versions"]}),
                "objectiveVersion": "priority-classes-then-min-changes-v1"}
    ledger = {"budget_bps": budget, "reported_wire_bps": reported,
              "confirmed_wire_bps": dict(confirmed_load), "uncertain_held_service_ids": sorted(uncertain),
              "superseded_release_before_reuse_bps": {k: v for k, v in conditional.items() if v},
              "fixed_outside_model_bps": {k: v for k, v in fixed.items() if v}}
    return snapshot, ledger


def _diagnose(state, snapshot, ledger, config, allowed, protected):
    authority = default_authority(snapshot, config)
    details = assess(snapshot["nodes"], snapshot["links"], snapshot["requests"],
                     snapshot["assignments"], snapshot["observedAtSec"])["services"]
    healthy = set(authority["frozenHealthyServiceIds"])
    rows, authorized, refresh = {}, [], []
    for sid in sorted(set(state["services"]) | set(state["confirmed"]) | allowed):
        demand, old = state["services"].get(sid), state["confirmed"].get(sid)
        evidence = []
        if demand is None:
            status = "demand_missing"
        elif not demand["fresh"]:
            status = "demand_stale"
        elif not demand["registered"] or not demand["active"]:
            status = "reported_inactive" if old else "inactive_unassigned"
        elif old is None:
            status = "observed_pending_admission"
        elif _superseded(demand, old):
            status = "observed_pending_new_demand_revision"
            evidence.append({"reason": "source_permit_revoked_on_demand_change",
                             "previous_revision": old["decision"]["demand_revision"],
                             "received_revision": demand["revision"]})
        elif demand["revision"] < old["decision"]["demand_revision"]:
            status = "demand_older_than_confirmed"
        else:
            lids = old["decision"]["link_path"]
            for lid in lids:
                row = state["links"][lid]
                if not row["usable"] and row.get("reason") in DOWN:
                    evidence.append({"link_id": lid, "reason": row["reason"]})
                elif row["usable"] and ledger["confirmed_wire_bps"].get(lid, 0) > row["capacity_bps"]:
                    evidence.append({"link_id": lid, "reason": "confirmed_load_exceeds_observed_capacity"})
            all_fresh = all(state["links"][lid]["usable"] for lid in lids)
            if all_fresh:
                _, violated = _quality(SimpleNamespace(**demand["service"]), lids, state["links"], 1036,
                                       qos_profile="official")
                evidence.extend({"reason": v} for v in violated)
            if evidence and sid not in healthy:
                status = "observed_physical_failure"
            elif not all_fresh:
                status = "path_observation_incomplete"
            elif not old["configuration_evidence"]["verified"]:
                status = "configuration_unverified_hold"
            elif sid in healthy and not evidence:
                status = "configuration_qos_passed"
            else:
                status = "held_without_b03_physical_failure_authority"
        potential = status
        eligible = (sid in allowed and sid not in protected
                    and status in {"reported_inactive", "observed_pending_admission",
                                   "observed_pending_new_demand_revision", "observed_physical_failure"})
        requires_refresh = bool(eligible and old and not old["configuration_evidence"]["verified"])
        if requires_refresh:
            refresh.append(sid)
            status = "configuration_refresh_required"
        can_change = eligible and not requires_refresh
        if can_change:
            authorized.append(sid)
        rows[sid] = {"status": status, "allowed": sid in allowed, "protected": sid in protected,
                     "authorized": can_change, "requires_refresh": requires_refresh,
                     "potential_action_status": potential, "evidence": evidence,
                     "configuration_evidence": old["configuration_evidence"] if old else None,
                     "b03_reasons": details.get(sid, {}).get("reasons", [])}
    authority.update(policyId="b03.packet.reported-failure-and-admission.v1",
                     policyVersion="wire-shared-healthy-freeze.v1", executableServiceIds=authorized)
    return {"authority": authority, "services": rows, "refresh_ids": refresh,
            "held_confirmed": sorted(set(state["confirmed"]) - set(authorized)),
            "protected_ids": sorted(protected), "allowed_ids": sorted(allowed)}


def _configuration(protected, snapshot):
    return dict(CONFIG, protected_service_ids=sorted(protected & set(snapshot["requests"])))


def _bound(work_id, state, config, diagnosis, snapshot, policy_hash):
    identity = {"workOrderId": work_id, "sourceSnapshotHash": digest(state),
                "physicalStateHash": digest(snapshot), "policyHash": policy_hash,
                "authorityHash": digest(diagnosis["authority"])}
    return {"identity": identity, "snapshot": snapshot, "config": config,
            "authority": diagnosis["authority"]}


def _device_scope(bound, state, target):
    # Reapply the original candidate_scope to the actual device configurations,
    # including demand revision. Same-path reinstalls can change the epoch permit.
    writable = set(bound["authority"]["executableServiceIds"])
    before, desired = {}, {}
    for sid in bound["snapshot"]["requests"]:
        ack = state["confirmed"].get(sid)
        before[sid] = (dict(_assignment(ack["decision"]), demandRevision=ack["decision"]["demand_revision"])
                       if ack else None)
        desired[sid] = before[sid]
        if sid in writable:
            desired[sid] = (dict(target[sid], demandRevision=state["services"][sid]["revision"])
                            if target[sid] is not None else None)
    physical = dict(bound, snapshot=dict(bound["snapshot"], assignments=before))
    return candidate_scope(physical, physical, desired)


def _resource(work_id, state, model, ledger, config, diagnosis, policy_hash, chooser):
    b = _bound(work_id, state, config, diagnosis, model, policy_hash)
    writable = diagnosis["authority"]["executableServiceIds"]
    selection = {"method": "lifecycle", "selected": None, "selectionWallNs": 0, "modelCalled": False}
    if writable:
        catalog = enumerate_actions(model, b["authority"], config)
        active_write = any(state["services"].get(sid, {}).get("active") for sid in writable)
        if active_write:
            action, selection = chooser.select(catalog, state, model, ledger, diagnosis, config)
        else:
            action = next(a for a in catalog if a["id"] == "I")
            selection["selected"] = "I"
        started = time.perf_counter_ns()
        target, audit = solve_action(action, b, chooser.context)
        if active_write and not chooser.opening_checked:
            from opening import check_opening
            old_target_hash = digest(target)
            target, admission = check_opening(target, model, config, b["authority"], chooser.initial_admission, action["id"]=="A")
            if admission["applied"]:
                guard = validate_proposal(model,config,b["authority"],target)
                if not guard["valid"]:
                    raise ValueError("Opening supplement failed the original proposal guard")
                audit["toolOnlyTargetHash"] = old_target_hash
                audit["targetHash"] = digest(target)
            audit["openingAdmission"] = admission
            chooser.opening_checked = True
    else:
        target = deepcopy(model["assignments"])
        audit = {"solverStatus": "no_authorized_work", "action": None, "pathSearch": {},
                 "proposedActions": [], "prefixRisk": [], "globallyOptimal": False}
    elapsed = time.perf_counter_ns() - started if writable else 0
    choices = {}
    rejected = []
    for sid in writable:
        row = state["services"][sid]
        reason = ("reported_inactive" if diagnosis["services"][sid]["status"] == "reported_inactive"
                  else "no_feasible_b03_candidate")
        choices[sid] = _choice(row["service"], target[sid], state["links"],
                               revision=row["revision"], reason=reason)
        # Guard the packet contract's strict threshold comparison as well as B03's EPS.
        if target[sid] is not None and not choices[sid]["accepted"] and target[sid] != model["assignments"][sid]:
            rejected.append(sid)
            target[sid] = None
    if rejected:
        check = validate_proposal(model, config, b["authority"], target)
        if not check["valid"]:
            raise ValueError("Packet QoS guard left an invalid B03 proposal")
        audit.update(check, strictPacketQosRejected=rejected, targetHash=digest(target))
    logical_scope = candidate_scope(b, b, target)
    scope = _device_scope(b, state, target)
    release_ids = [sid for sid in scope["changedServiceIds"] if sid in state["confirmed"]]
    install_ids = [sid for sid in scope["changedServiceIds"] if target[sid] is not None]
    if set(scope["changedServiceIds"]) - set(writable):
        raise ValueError("Changed service outside work-order authority")
    final_load = loads(model["links"], target)
    ledger = dict(ledger, proposed_wire_bps={
        lid: load + ledger["fixed_outside_model_bps"].get(lid, 0)
        for lid, load in final_load.items() if load or ledger["fixed_outside_model_bps"].get(lid, 0)})
    return {"work_id": work_id, "snapshot_id": digest(state), "policy_hash": policy_hash,
            "decisions": choices, "authorized": list(writable), "solver_wall_ns": elapsed,
            "decision_wall_ns": elapsed + selection["selectionWallNs"], "tool_choice": selection,
            "release_ids": release_ids, "install_ids": install_ids, "refresh_ids": diagnosis["refresh_ids"],
            "scope": scope, "solver_scope": logical_scope,
            "target": target, "wire_ledger": ledger, "solver_audit": audit,
            "real_solver_calls": int(bool(writable))}



def _same_config(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and _signature(left) == _signature(right)


def _installed_path(choice, node, result):
    if result.get("retired") is not False:
        return False
    path = choice["node_path"]
    if node == path[-1]:
        return all(k in result and result[k] is None for k in ("prepared", "installed", "forwarding"))
    i = path.index(node)
    hop = {"next_node_id": path[i + 1], "link_id": choice["link_path"][i]}
    p = result.get("prepared")
    return (result.get("forwarding") == hop and isinstance(p, dict) and p == result.get("installed")
            and all(p.get(k) == v for k, v in hop.items()) and p.get("reserved_bps") == choice["reserved_bps"])


def _check_local_rule(phase, choice, epoch, node, result):
    keys = {"source", "prepared", "installed", "forwarding", "retired"}
    if not isinstance(result, dict) or not keys <= result.keys():
        return False
    path, src = choice["node_path"], result["source"]
    if (node == path[0]) != isinstance(src, dict):
        return False
    if phase == "release":
        return (result["retired"] is True
                and all(result[k] is None for k in ("prepared", "installed", "forwarding"))
                and (src is None or (src.get("enabled") is False and src.get("rate_bps") == 0)))
    if src is not None:
        if not (src.get("enabled") is True and src.get("active") is True
                and src.get("registered") is True and src.get("demand_active") is True
                and src.get("epoch") == epoch == src.get("active_epoch")
                and src.get("rate_bps") == choice["reserved_bps"]
                and src.get("demand_revision") == choice["demand_revision"]):
            return False
    return _installed_path(choice, node, result)


def _configuration_evidence(choice, epoch, samples, demand, probes, floors, now, ttl):
    """Bind every installed hop to an arrived query and a time or version witness."""
    path = choice["node_path"]
    demand_fresh = demand is not None and demand["fresh"]
    active_epoch = bool(demand_fresh and demand["registered"] and demand["active"]
                        and demand["revision"] == choice["demand_revision"])
    revoked_epoch = bool(demand_fresh and demand["revision"] > choice["demand_revision"])
    mode = "active_original_permit" if active_epoch else "revoked_permit_old_rules" if revoked_epoch else "unknown"
    details = {}
    for nid in path:
        rec, probe = samples.get(nid), probes.get(nid)
        node = {"status": "missing_readback", "delivered_version_floor": floors.get(nid),
                "probe": {k: probe[k] for k in ("version", "sampled_at_ns", "received_at_ns", "seq")} if probe else None}
        details[nid] = node
        if rec is None:
            continue
        sampled, arrived, rev = rec["observed_at_ns"], rec["arrived_at_ns"], rec.get("version")
        node.update(readback={k: rec.get(k) for k in ("query_id", "phase", "observed_at_ns",
                                                       "arrived_at_ns", "version")})
        issued = rec.get("issued_at_ns")
        if (rec.get("service_id") != choice["service_id"] or rec.get("epoch") != epoch
                or rec.get("node_id") != nid or rec.get("network_source") != nid
                or not rec.get("work_id") or not rec.get("query_id") or type(rev) is not int
                or type(issued) is not int or not 0 <= issued <= sampled <= arrived <= now
                or rec.get("phase") not in ("install", "config_refresh") or rec.get("matched") is False):
            node["status"] = "readback_mismatch"
            continue
        state = rec.get("result")
        src = state.get("source") if isinstance(state, dict) else None
        if not isinstance(state, dict) or not _installed_path(choice, nid, state):
            node["status"] = "readback_mismatch"
            continue
        if nid == path[0]:
            if active_epoch:
                matches = _check_local_rule("install", choice, epoch, nid, state)
            elif revoked_epoch:
                # Demand callbacks revoke the source permit without bumping node version.
                # A new source sample for this revision is therefore indispensable.
                matches = (rec["phase"] == "config_refresh" and isinstance(src, dict)
                           and src.get("enabled") is False and src.get("rate_bps") == 0
                           and src.get("active_epoch") == epoch == src.get("epoch")
                           and src.get("demand_revision") == demand["revision"]
                           and src.get("registered") is demand["registered"]
                           and src.get("demand_active") is demand["active"])
            else:
                matches = False
            if not matches:
                node["status"] = "demand_revision_or_source_unverified"
                continue
        elif src is not None:
            node["status"] = "readback_mismatch"
            continue
        # A delivered later version invalidates even a recent query for that device.
        if (floors.get(nid, rev) > rev or (probe and
                (probe["version"] > rev or (probe["sampled_at_ns"] >= sampled and probe["version"] != rev)))):
            node["status"] = "node_version_changed"
        elif now - sampled <= ttl:
            node["status"] = "fresh_readback"
        elif probe and probe["version"] == rev and probe["sampled_at_ns"] >= sampled:
            node["status"] = "version_continuity"
        else:
            node["status"] = "stale_readback"
    states = {item["status"] for item in details.values()}
    verified = bool(path) and states <= {"fresh_readback", "version_continuity"} and (active_epoch or revoked_epoch)
    if verified:
        status = "version_continuity" if "version_continuity" in states else "fresh_readback"
    elif states & {"node_version_changed", "readback_mismatch"}:
        status = "mismatch"
    elif "stale_readback" in states:
        status = "stale"
    else:
        status = "unknown"
    return {"status": status, "verified": bool(verified), "source_mode": mode, "nodes": details,
            "demand_revision": demand["revision"] if demand_fresh else None}


def _delivered_receipts(receipts, now_ns):
    delivered = {}
    for sid, phases in (receipts or {}).items():
        for phase, batch in phases.items():
            nodes = {}
            for nid, row in batch.get("nodes", {}).items():
                arrived, sampled = row.get("arrived_at_ns"), row.get("observed_at_ns")
                if type(arrived) is int and type(sampled) is int and 0 <= sampled <= arrived <= now_ns:
                    nodes[nid] = deepcopy(row)
            if nodes:
                delivered.setdefault(sid, {})[phase] = {"epoch": batch["epoch"],
                                                        "decision": _signature(batch["decision"]),
                                                        "nodes": nodes}
    return delivered


def _verify_order(work_id, saved, state, receipts, expected, operations):
    now = state["now_ns"]
    if now < saved["time_ns"]:
        raise ValueError("Verification precedes the work order")
    changed = sorted(set(saved["release_ids"]) | set(saved["install_ids"]))
    matched, missing, mismatched, rows = [], [], [], {}
    seen_queries = set()
    for sid in changed:
        d, ack = saved["decisions"][sid], state["confirmed"].get(sid)
        absent, wrong = [], []
        if expected is not None and not _same_config(expected.get(sid), d):
            wrong.append("expected_decision_differs_from_original_rc_artifact")
        if sid in saved["install_ids"]:
            if ack is None:
                absent.append("controller_activation_ack")
            elif not _same_config(ack["decision"], d):
                wrong.append("controller_confirmed_different_config")
        elif ack is not None:
            wrong.append("controller_still_confirms_released_config")
        phase_summaries = {}
        for phase in ("release", "install"):
            if sid not in saved[phase + "_ids"]:
                continue
            report = receipts.get(sid, {}).get(phase)
            choice = saved["old"][sid]["decision"] if phase == "release" else d
            expected_epoch = (saved["old"][sid]["epoch"] if phase == "release"
                              else None if ack is None else ack["epoch"])
            if not isinstance(report, dict):
                absent.append(phase + "_device_queries")
                continue
            epoch = report.get("epoch")
            if (type(epoch) is not int or epoch != expected_epoch
                    or not _same_config(report.get("decision"), choice)
                    or (phase == "install" and sid in saved["old"] and epoch <= saved["old"][sid]["epoch"])):
                wrong.append(phase + "_decision_or_epoch")
            queried = report.get("nodes", {})
            if not isinstance(queried, dict):
                wrong.append(phase + "_invalid_nodes")
                continue
            if set(queried) - set(choice["node_path"]):
                wrong.append(phase + "_off_path_query")
            node_rows = {}
            for nid in choice["node_path"]:
                rec = queried.get(nid)
                label = phase + "/" + nid
                if not isinstance(rec, dict):
                    absent.append(label)
                    continue
                query_id = rec.get("query_id")
                times = [rec.get(k) for k in ("issued_at_ns", "observed_at_ns", "arrived_at_ns")]
                valid = (rec.get("work_id") == work_id and rec.get("phase") == phase
                         and rec.get("service_id") == sid and rec.get("epoch") == epoch
                         and rec.get("node_id") == nid == rec.get("network_source")
                         and type(rec.get("version")) is int and rec["version"] >= 0
                         and isinstance(query_id, str) and bool(query_id) and query_id not in seen_queries
                         and all(type(t) is int for t in times)
                         and saved["time_ns"] <= times[0] <= times[1] <= times[2] <= now
                         and (phase == "release" or now - times[1] <= state["link_ttl_ns"])
                         and _check_local_rule(phase, choice, epoch, nid, rec.get("result")))
                if not valid:
                    wrong.append(label)
                if isinstance(query_id, str):
                    seen_queries.add(query_id)
                node_rows[nid] = {"query_id": query_id, "observed_at_ns": times[1],
                                  "arrived_at_ns": times[2], "matched": valid}
            phase_summaries[phase] = {"epoch": epoch, "nodes": node_rows}
            if operations is not None and phase not in operations.get("completed", {}).get(sid, []):
                absent.append(phase + "_device_completion")
        failure = (operations or {}).get("failures", {}).get(sid)
        if failure:
            wrong.append("device_operation_failed")
        current = state["services"].get(sid)
        latest = "unknown"
        if current and current["fresh"]:
            if not (current["active"] and current["registered"]):
                latest = "reported_inactive"
            elif ack is None:
                latest = "active_unassigned"
            elif not all(state["links"][lid]["usable"] for lid in ack["decision"]["link_path"]):
                latest = "path_unavailable_or_unobserved"
            else:
                _, violations = _quality(SimpleNamespace(**current["service"]), ack["decision"]["link_path"],
                                         state["links"], 1036, qos_profile="official")
                latest = "fresh_path_qos_violations" if violations else "fresh_path_configured_qos_passed"
        if absent:
            missing.append(sid)
        if wrong:
            mismatched.append(sid)
        if not wrong and not absent:
            matched.append(sid)
        rows[sid] = {"matched": not (wrong or absent), "missing": absent, "mismatched": wrong,
                     "phases": phase_summaries, "operation_failure": failure, "demand_and_path": latest}
    unexpected = sorted((set(receipts) | set((operations or {}).get("completed", {}))) - set(changed))
    return {"work_id": work_id, "source_snapshot_id": saved["snapshot_id"], "snapshot_id": digest(state),
            "unexpected_services": unexpected, "device_receipts_sha256": digest(receipts),
            "configuration_matches": (len(matched) == len(changed) and not unexpected) if changed or unexpected else None,
            "matched": matched, "unconfirmed": sorted(set(changed) - set(matched)),
            "missing": missing, "mismatched": mismatched, "details": rows,
            "evidence_source": "delivered_device_query_replies_and_controller_confirmed",
            "business_delivery_measured": False, "read_only": True}


class _PacketAgents:
    """Role endpoints see detached JSON and their immutable policy. Neither role owns a writer."""

    def __init__(self, allowed, protected, headroom, chooser):
        self.chooser = chooser
        self.allowed = None if allowed is None else frozenset(allowed)
        self.protected = frozenset(protected)
        self.headroom = headroom
        self.policy_hash = digest({"policy": "b03.packet.reported-failure-and-admission.v1",
                                   "allowed": "all_arrived_registered" if self.allowed is None else sorted(self.allowed),
                                   "protected": sorted(self.protected),
                                   "config_evidence": "per_node_query_ttl_or_unchanged_fresh_version.v1",
                                   "headroom": headroom, "method": chooser.identity, "config": CONFIG})
        self.exchange = None
        self.orders = {}

    def scopes(self, state):
        if self.allowed is not None:
            return self.allowed, self.protected
        known = set(state["services"]) | set(state["confirmed"])
        eligible = {sid for sid, row in state["services"].items() if row["registered"]} | set(state["confirmed"])
        return eligible, self.protected & known

    def handle(self, request):
        kind, wid, p = request["kind"], request["work_id"], request["payload"]
        state = p["state"]
        if p.get("policy_hash") != self.policy_hash or digest(state) != request["snapshot_id"]:
            raise ValueError("Work-order policy or source snapshot binding failed")
        if kind == "verify":
            saved = self.orders[wid]
            if request["parent_message_id"] != saved["observe_message_id"]:
                raise ValueError("Verification not correlated to original order")
            return _verify_order(wid, saved, state, p["device_receipts"], p["expected"], p["operations"])
        allowed, protected = self.scopes(state)
        model, ledger = _model(state, self.headroom, allowed - protected)
        config = _configuration(protected, model)
        diagnosis = _diagnose(state, model, ledger, config, allowed, protected)
        diagnosis["authority_mode"] = "all_arrived_registered" if self.allowed is None else "explicit_whitelist"
        if kind == "control_request":
            if diagnosis != p["diagnosis"] or not request["parent_message_id"]:
                raise ValueError("RC cannot reproduce OM authority on this snapshot")
            return _resource(wid, state, model, ledger, config, diagnosis, self.policy_hash, self.chooser)
        if kind != "observe":
            raise ValueError("Unknown packet role operation")
        data = {"state": state, "policy_hash": self.policy_hash, "diagnosis": diagnosis}
        res, receipt = self.exchange("control_request", wid, request["snapshot_id"], data,
                                     parent_message_id=request["message_id"])
        target = res.pop("target")
        bound = _bound(wid, state, config, diagnosis, model, self.policy_hash)
        scope = _device_scope(bound, state, target)
        if (res["scope"] != scope or res["solver_scope"] != candidate_scope(bound, bound, target) or res["authorized"] != diagnosis["authority"]["executableServiceIds"]
                or res["refresh_ids"] != diagnosis["refresh_ids"]
                or set(res["refresh_ids"]) & set(res["authorized"])
                or set(res["release_ids"]) | set(res["install_ids"]) != set(scope["changedServiceIds"])
                or res["work_id"] != wid or res["snapshot_id"] != request["snapshot_id"]
                or res["policy_hash"] != self.policy_hash):
            raise ValueError("OM rejected unbound RC result or action scope")
        for sid in res["install_ids"]:
            row = state["services"][sid]
            choice = _choice(row["service"], target[sid], state["links"], revision=row["revision"])
            if not choice["accepted"] or not _same_config(res["decisions"][sid], choice):
                raise ValueError("RC install differs from its B03 candidate or original demand")
        changed = scope["changedServiceIds"]
        self.orders[wid] = {"snapshot_id": request["snapshot_id"], "time_ns": state["now_ns"],
                            "observe_message_id": request["message_id"], "release_ids": res["release_ids"],
                            "install_ids": res["install_ids"], "decisions": {sid: res["decisions"][sid] for sid in changed},
                            "old": {sid: state["confirmed"][sid] for sid in res["release_ids"]}}
        return dict(res, diagnosis=diagnosis, resource_a2a=receipt)


class PacketRoles:
    """Controller port for work-order planning and verification from received cache copies."""

    def __init__(self, allowed_ids, log, *, control_headroom_fraction=.05, protected_ids=(),
                 urls=None, run_id=None, chooser=None):
        from frozen_policy import DecisionPolicy
        options = _packet_options(1000, 8, control_headroom_fraction)
        self._agents = _PacketAgents(allowed_ids, protected_ids, options["control_headroom_fraction"], chooser or DecisionPolicy("I"))
        self._log = log
        self._run = run_id or uuid.uuid4().hex
        self._seq = 0
        self._messages = {}
        self._urls = urls
        self._transport = None
        self._closed = False

    def release_authority(self, now_ns, links_snapshot, demands_snapshot, confirmed, retained_wire_bps, sid):
        """Recheck the OM stop permit against delivered evidence immediately before dispatch."""
        state = _state(now_ns, links_snapshot, demands_snapshot, confirmed, retained_wire_bps)
        allowed, protected = self._agents.scopes(state)
        model, ledger = _model(state, self._agents.headroom, allowed - protected)
        config = _configuration(protected, model)
        diagnosis = _diagnose(state, model, ledger, config, allowed, protected)
        row = diagnosis["services"].get(sid, {})
        return {"authorized": sid in state["confirmed"] and row.get("authorized") is True,
                "status": row.get("status", "demand_missing"), "requires_refresh": row.get("requires_refresh", False),
                "refresh_required": row.get("requires_refresh", False), "snapshot_id": digest(state)}

    def planning_key(self):
        """A stable identity for this runtime's immutable whitelist, method and budgets."""
        return self._agents.policy_hash

    def _exchange(self, kind, wid, state, payload, parent=None):
        if self._closed:
            raise RuntimeError("PacketRoles is closed")
        if self._transport is None:
            self._transport = PacketA2A(self._agents.handle, self._urls)
            self._agents.exchange = self._transport.exchange
        return self._transport.exchange(kind, wid, digest(state),
                                        dict(payload, state=state, policy_hash=self._agents.policy_hash),
                                        parent_message_id=parent)

    def plan(self, now_ns, links_snapshot, demands_snapshot, confirmed, retained_wire_bps):
        state = _state(now_ns, links_snapshot, demands_snapshot, confirmed, retained_wire_bps)
        self._seq += 1
        wid = f"{self._run}-W{self._seq:06d}"
        plan, om = self._exchange("observe", wid, state, {})
        rc = plan.pop("resource_a2a")
        allowed, protected = self._agents.scopes(state)
        if (plan["work_id"] != wid or plan["snapshot_id"] != digest(state)
                or plan["policy_hash"] != self._agents.policy_hash
                or not set(plan["authorized"]) <= allowed - protected):
            raise ValueError("Controller rejected work-order binding or executable scope")
        self._messages[wid] = om["message_id"]
        if self._seq == 1:
            self._log({"event": "b03_runtime", "time_ns": now_ns, "implementations": _implementations(),
                       "role_schema": "b03.packet.role.v1", "policy_hash": self.planning_key(),
                       "configuration_evidence": "per_node_query_ttl_or_unchanged_fresh_version",
                       "protocol": om["protocol"], "binding": om["binding"], "sdk_version": om["sdk_version"],
                       "task_store": "separate_in_memory_per_role", "process_restart_recovery": False})
        if plan["release_ids"] or plan["install_ids"] or plan["refresh_ids"]:
            self._log({"event": "b03_observation_evidence", "purpose": "plan", "time_ns": now_ns,
                       "work_id": wid, "snapshot_id": plan["snapshot_id"], "state": state})
        self._log({"event": "b03_om_diagnosis", "time_ns": now_ns, "work_id": wid,
                   "snapshot_id": plan["snapshot_id"], **plan["diagnosis"]})
        for receipt in (rc, om):
            self._log({"event": "b03_a2a", "time_ns": now_ns, **receipt})
        self._log({"event": "b03_rc_plan", "time_ns": now_ns, **{k: v for k, v in plan.items() if k != "diagnosis"}})
        return plan

    def verify(self, work_id, now_ns, links_snapshot, demands_snapshot, confirmed, *,
               device_receipts=None, expected=None, operations=None, retained_wire_bps=None):
        state = _state(now_ns, links_snapshot, demands_snapshot, confirmed, retained_wire_bps)
        payload = {"device_receipts": _delivered_receipts(device_receipts, now_ns),
                   "expected": deepcopy(expected), "operations": deepcopy(operations)}
        result, om = self._exchange("verify", work_id, state, payload, self._messages[work_id])
        self._log({"event": "b03_observation_evidence", "purpose": "verify", "time_ns": now_ns,
                   "work_id": work_id, "snapshot_id": result["snapshot_id"], "state": state,
                   "device_receipts": payload["device_receipts"], "operations": payload["operations"]})
        self._log({"event": "b03_a2a", "time_ns": now_ns, **om})
        self._log({"event": "b03_om_verify", "time_ns": now_ns, **result})
        return result

    def close(self):
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._agents.exchange = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
