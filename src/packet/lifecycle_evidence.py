"""Offline checks for controller-owned expiry, device retirement and the next delivered work order."""
from collections import Counter, defaultdict


def _require(ok, wid, message):
    if not ok:
        raise ValueError(f"Lifecycle work {wid}: {message}")


def _plan_state(plan, events):
    wid = plan["work_id"]
    rows = [e for e in events["b03_observation_evidence"] if e["purpose"] == "plan"]
    _require(len(rows) == 1 and rows[0]["snapshot_id"] == plan["snapshot_id"]
             and rows[0]["time_ns"] == plan["time_ns"], wid, "missing matching delivered planning snapshot")
    return rows[0]["state"]


def _check_demand(plan, state, sid, reports, ttl):
    wid = plan["work_id"]
    row = state["services"].get(sid)
    _require(row is not None, wid, f"missing delivered demand for {sid}")
    source = reports.get((sid, row["seq"]))
    _require(source is not None, wid, f"missing native demand receipt for {sid}")
    msg, at = source["message"], plan["time_ns"]
    _require(source["result"]["accepted"] and source["time_ns"] == row["received_at_ns"]
             and msg["service"]["service_id"] == sid and msg["node_id"] == row["node_id"]
             and all(msg[k] == row[k] for k in ("active", "registered", "revision", "observed_at_ns"))
             and 0 <= row["observed_at_ns"] <= row["received_at_ns"] <= at
             and row["fresh"] is (at - row["observed_at_ns"] <= ttl), wid, f"demand differs from native receipt for {sid}")
    return row


def _check_old_config(plan, old, demand, reads):
    wid, choice = plan["work_id"], old["decision"]
    sid, path = choice["service_id"], choice["node_path"]
    witness = old["configuration_evidence"]
    _require(witness["verified"] and witness["source_mode"] == "revoked_permit_old_rules"
             and demand["revision"] > choice["demand_revision"], wid, f"unverified old epoch for {sid}")
    for node in path:
        ref = old["readback_nodes"].get(node)
        actual = reads.get(ref["query_id"]) if ref else None
        _require(actual is not None and actual["matched"] and actual["epoch"] == old["epoch"]
                 and actual["service_id"] == sid and actual["node_id"] == node == actual["network_source"]
                 and actual["arrived_at_ns"] <= plan["time_ns"]
                 and all(actual[k] == ref[k] for k in ("work_id", "phase", "observed_at_ns", "arrived_at_ns", "result"))
                 and witness["nodes"][node]["status"] in {"fresh_readback", "version_continuity"},
                 wid, f"old configuration lacks delivered readback at {sid}/{node}")
        if node == path[0]:
            source = actual["result"]["source"]
            _require(actual["phase"] == "config_refresh" and source["enabled"] is False and source["rate_bps"] == 0
                     and source["demand_revision"] == demand["revision"]
                     and source["registered"] is demand["registered"] and source["demand_active"] is demand["active"],
                     wid, f"old source permit still active or unconfirmed for {sid}")


def _check_retired(plan, state, event, events, reads, before, recovered_balance):
    wid, sid = plan["work_id"], event["service_id"]
    old = state["confirmed"][sid]
    choice, epoch = old["decision"], old["epoch"]
    path = choice["node_path"]
    steps = [e for e in events["work_step"] if e["service_id"] == sid and e["phase"] == "release"]
    _require(len(steps) == 1 and steps[0]["plan_id"] == event["plan_id"]
             and all(event["decision"][k] == v == steps[0]["decision"][k] for k, v in choice.items())
             and steps[0]["time_ns"] >= plan["time_ns"] + plan["decision_wall_ns"]
             and event["epoch"] == epoch, wid, f"retirement differs from device plan for {sid}")
    step = steps[0]
    _require(before == Counter(step["retained_wire_bps"]), wid, f"pre-release reservation differs for {sid}")
    requested = [e for e in events["readback_requested"] if e["service_id"] == sid and e["phase"] == "release"]
    _require(requested and all(e["epoch"] == epoch and set(e["nodes"]) == set(path)
             and (recovered_balance is not None or Counter(e["retained_wire_bps"]) == before) for e in requested), wid, f"reservation lost before readback for {sid}")
    authority = [e for e in events["release_authority_checked"] if e["service_id"] == sid]
    _require(any(e["status"] == "reported_inactive" and e["time_ns"] == step["time_ns"]
                 and e["demand_revision"] == state["services"][sid]["revision"] for e in authority),
             wid, f"missing dispatch-time release permission for {sid}")
    writes = [(c["op"], c["node_id"]) for c in events["device_command"]
              if c["service_id"] == sid and c["op"] != "query"]
    _require(("deactivate", path[0]) in writes and {n for op, n in writes if op == "cleanup"} == set(path),
             wid, f"missing stop or path cleanup for {sid}")
    _require(set(event["query_ids"]) == set(path), wid, f"partial release receipt for {sid}")
    for node, qid in event["query_ids"].items():
        r = reads.get(qid)
        _require(r is not None and r["work_id"] == wid and r["phase"] == "release" and r["matched"]
                 and r["service_id"] == sid and r["node_id"] == node == r["network_source"] and r["epoch"] == epoch
                 and step["time_ns"] <= r["issued_at_ns"] <= r["observed_at_ns"] <= r["arrived_at_ns"] <= event["time_ns"],
                 wid, f"release lacks a matching delivered query for {sid}/{node}")
        result = r["result"]
        source = result["source"]
        _require(result["retired"] is True and all(result[k] is None for k in ("prepared", "installed", "forwarding"))
                 and (source is None if node != path[0] else source is not None and
                      source["enabled"] is False and source["rate_bps"] == 0), wid, f"device retained old epoch for {sid}/{node}")
    if recovered_balance is not None:
        # The independent trace audit checked every intermediate full ledger after the yield.
        before.clear()
        before.update(recovered_balance)
    for lid in choice["link_path"]:
        _require(before[lid] >= choice["wire_bps"], wid, f"old reservation missing on {lid}")
        before[lid] -= choice["wire_bps"]
    _require(before == Counter(event["retained_wire_bps"]), wid, f"incorrect released wire total for {sid}")


def check_lifecycle(plans, acts, demand_index, conditions, probe_index=None):
    """Check current receipts. Historical component-v1 archives use their original lifecycle contract."""
    counts = dict(releaseOrders=0, confirmedReleases=0, followupOrders=0,
                  activeServicesReassessed=0, pendingReleaseOrders=0, pendingReassessments=0)
    plans = [p for p in plans if p["event"] == "b03_rc_plan"]
    current = conditions.get("provenance", {}).get("schema") == "b03.provenance.v2"
    if not plans or not (current or any(p.get("lifecycle_release") or p.get("reassessment_after_release") for p in plans)
                         or any(e.get("lifecycle_policy") == "controller-confirmed-expiry.v1" for e in acts)):
        return counts
    grouped = defaultdict(lambda: defaultdict(list))
    reads, triggers = {}, set()
    for event in acts:
        grouped[event.get("work_id")][event["event"]].append(event)
        if event["event"] == "device_readback":
            reads[event["query_id"]] = event
        if event["event"] == "work_trigger" and "lifecycle_release_confirmed" in event["reasons"]:
            triggers.add(event["time_ns"])

    permissions = conditions["authority"]
    writable = set(permissions["allowedServiceIds"]) - set(permissions.get("protectedServiceIds", ()))
    ttl = conditions["simulation"]["control"]["observation_ttl_ns"]
    from packet_cleanup_evidence import cleanup_budgets
    recovered = cleanup_budgets(acts, grouped, conditions, probe_index or {})
    interleaved = any(e["event"] == "cleanup_deferred" for e in acts)
    by_next_plan = {}
    unsigned_followups = 0
    if interleaved:
        # A parked order may finish after a newer one. Bind each sign-off to the actual
        # next planned snapshot in trace order, including equal-clock events.
        previous_signoff = None
        for row in acts:
            if row["event"] in ("work_verified", "work_incomplete"):
                history = grouped[row["work_id"]]["b03_rc_plan"]
                stage = history[0].get("lifecycle_release") if history else None
                matched = sorted(row["om"]["matched"]) if stage else []
                if matched:
                    _require(previous_signoff is None, row["work_id"], "missing reassessment before the next sign-off")
                    previous_signoff = ({"work_id": row["work_id"], "verified_at_ns": row["time_ns"], "release_ids": matched},
                                        stage["deferred_active_ids"], row["retained_wire_bps"])
            elif row["event"] == "b03_rc_plan" and previous_signoff:
                by_next_plan[row["work_id"]] = previous_signoff
                previous_signoff = None
        unsigned_followups = int(previous_signoff is not None)
    pending = None
    for plan in plans:
        wid = plan["work_id"]
        events = grouped[wid]
        dx = events["b03_om_diagnosis"]
        _require(len(dx) == 1 and dx[0]["snapshot_id"] == plan["snapshot_id"], wid, "missing OM diagnosis")
        staged = plan.get("lifecycle_release")
        marker = plan.get("reassessment_after_release")
        if interleaved:
            pending = by_next_plan.get(wid)
        if pending:
            expected, deferred, budgets = pending
            _require(marker == expected and plan["time_ns"] >= expected["verified_at_ns"]
                     and plan["time_ns"] in triggers, wid, "next work order missing verified release or trigger")
            state = _plan_state(plan, events)
            _require(not set(expected["release_ids"]).intersection(state["confirmed"])
                     and Counter(state["retained_wire_bps"]) == Counter(budgets), wid, "reassessment used wrong reservations")
            for sid in deferred:
                _check_demand(plan, state, sid, demand_index, ttl)
                _require(sid in dx[0]["services"], wid, f"deferred service absent from OM reassessment: {sid}")
            counts["activeServicesReassessed"] += len(deferred)
            counts["followupOrders"] += 1
            pending = None
        else:
            _require(marker is None, wid, "unexpected preceding release")
        diagnosed = dx[0]["services"]
        due = {sid for sid, row in diagnosed.items() if row["status"] == "reported_inactive"
               and row["allowed"] and not row["protected"] and row["configuration_evidence"]["verified"]}
        _require(staged == dx[0].get("lifecycle_release") and bool(staged) == bool(due),
                 wid, "OM and RC disagree on the inactive release scope")
        if not staged:
            continue
        ids = sorted(due)
        deferred = sorted(s for s, r in diagnosed.items() if r["allowed"] and not r["protected"]
                          and r["status"] in {"observed_pending_admission", "observed_pending_new_demand_revision",
                                              "observed_physical_failure"})
        _require(staged["owner"] == "controller" and due <= writable and ids == staged["release_ids"]
                 == plan["release_ids"] == plan["authorized"] == dx[0]["authority"]["executableServiceIds"]
                 and deferred == staged["deferred_active_ids"]
                 and sorted(s for s, r in diagnosed.items() if r["requires_refresh"]) == staged["deferred_refresh_ids"]
                 and not plan["install_ids"] and not plan["refresh_ids"],
                 wid, "controller release includes active, held or unauthorized changes")
        _require(plan["tool_choice"] == {"method": "lifecycle", "modelCalled": False, "selected": None, "selectionWallNs": 0}
                 and plan["real_solver_calls"] == plan["solver_wall_ns"] == 0 and "component_io" not in plan
                 and plan["solver_audit"]["action"] is None and plan["solver_audit"]["solverStatus"] == "controller_lifecycle_release"
                 and type(plan["lifecycle_wall_ns"]) is int and plan["decision_wall_ns"] == plan["lifecycle_wall_ns"] >= 0,
                 wid, "expiry charged to a rule/model or missing controller time")
        state = _plan_state(plan, events)
        for sid in ids:
            demand = _check_demand(plan, state, sid, demand_index, ttl)
            _require(demand["fresh"] and not (demand["registered"] and demand["active"]),
                     wid, f"missing fresh withdrawal for {sid}")
            choice = plan["decisions"][sid]
            _require(choice["accepted"] is False and choice["reason"] == "reported_inactive"
                     and choice["demand_revision"] == demand["revision"], wid, f"invalid withdrawal decision for {sid}")
            _check_old_config(plan, state["confirmed"][sid], demand, reads)
        for sid in deferred:
            demand = _check_demand(plan, state, sid, demand_index, ttl)
            _require(demand["fresh"] and demand["registered"] and demand["active"]
                     and not diagnosed[sid]["authorized"], wid, f"invalid deferred active service {sid}")
        for command in events["device_command"]:
            if command["op"] == "query":
                continue
            sid, node = command["service_id"], command["node_id"]
            _require(sid in due, wid, f"unexpected device write during expiry for {sid}")
            old = state["confirmed"][sid]
            path = old["decision"]["node_path"]
            _require(command["epoch"] == old["epoch"]
                     and ((command["op"] == "deactivate" and node == path[0])
                          or (command["op"] == "cleanup" and node in path)), wid, f"unexpected expiry write for {sid}/{node}")
        counts["releaseOrders"] += 1
        held = Counter(state["retained_wire_bps"])
        if events["service_released"]:
            started = events["work_started"]
            _require(len(started) == 1 and started[0]["release_ids"] == ids
                     and started[0]["snapshot_id"] == plan["snapshot_id"]
                     and Counter(started[0]["budgets_before"]) == held, wid, "device plan or initial reservation differs")
        for event in events["service_released"]:
            _require(event["service_id"] in due, wid, "unplanned retirement")
            _check_retired(plan, state, event, events, reads, held, recovered.get(wid))
        ends = events["work_verified"] + events["work_incomplete"]
        _require(len(ends) <= 1, wid, "duplicate work completion")
        matched = sorted(ends[0]["om"]["matched"]) if ends else []
        retired = [e["service_id"] for e in events["service_released"]]
        _require(set(matched) <= set(retired) and len(set(retired)) == len(retired), wid, "invalid OM release sign-off")
        if events["work_verified"]:
            _require(matched == ids and ends[0]["om"]["configuration_matches"] is True, wid, "incomplete signed release")
        counts["confirmedReleases"] += len(matched)
        counts["pendingReleaseOrders"] += int(matched != ids)
        if matched:
            signed = ends[0]
            for event in events["service_released"]:
                sid = event["service_id"]
                if sid in matched:
                    phase = signed["om"]["details"][sid]["phases"]["release"]
                    _require(phase["epoch"] == event["epoch"]
                             and event["query_ids"] == {node: r["query_id"] for node, r in phase["nodes"].items()
                                                       if r["matched"]}, wid, f"OM signed different device receipts for {sid}")
            _require(all(e["time_ns"] <= signed["time_ns"] for e in events["service_released"]), wid, "premature OM sign-off")
            pending = ({"work_id": wid, "verified_at_ns": signed["time_ns"], "release_ids": matched},
                       deferred, signed["retained_wire_bps"])
    counts["pendingReassessments"] = unsigned_followups if interleaved else int(pending is not None)
    return counts
