"""Read-only lookups against a pinned technical edition and their use at RC opening."""
from copy import deepcopy
import hashlib
import json


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _entries(library, edition):
    if (not isinstance(library, dict) or library.get("schema") != "b03.knowledge.v1"
            or library.get("version") != edition or not isinstance(library.get("entries"), list)):
        raise ValueError("The configured technical knowledge edition differs from the source")
    entries = {}
    for row in library["entries"]:
        if (not isinstance(row, dict) or any(not _text(row.get(k)) for k in ("id", "content", "applicability"))
                or any(not isinstance(row.get(k), list) or not row[k] or not all(_text(x) for x in row[k])
                       for k in ("topics", "sources")) or row["id"] in entries):
            raise ValueError("Incomplete or duplicate knowledge entry")
        entry = {key: deepcopy(row[key]) for key in ("id", "topics", "content", "sources", "applicability")}
        entries[entry["id"]] = {**entry, "version": edition}
    return entries


def query_library(library, edition, *, identifier=None, topic=None):
    if (identifier is None) == (topic is None) or not _text(identifier if topic is None else topic):
        raise ValueError("Select exactly one nonempty knowledge ID or topic")
    entries = _entries(library, edition)
    if identifier is not None:
        return [entries[identifier]] if identifier in entries else []
    return [row for row in entries.values() if any(topic.casefold() in t.casefold() for t in row["topics"])]


def _lookup(components, identifier, topic):
    raw = components.knowledge_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    reg = components.knowledge
    library = json.loads(raw)
    expected = query_library(library, reg["version"], identifier=identifier, topic=topic)
    if reg.get("entrypoint", "builtin") == "builtin":
        return expected, source_hash
    config = {k: deepcopy(reg[k]) for k in ("id", "version", "entrypoint", "options") if k in reg}
    provider = components.factory("knowledge")(config=config, library=deepcopy(library))
    lookup = getattr(provider, "lookup", None)
    if not callable(lookup):
        raise ValueError("A knowledge provider must implement lookup")
    ids = lookup(identifier=identifier, topic=topic)
    matching = {row["id"]: row for row in expected}
    if (not isinstance(ids, list) or not all(_text(i) for i in ids)
            or len(ids) != len(matching) or set(ids) != matching.keys()):
        raise ValueError("Provider IDs differ from the lookup in the pinned edition")
    return [matching[i] for i in ids], source_hash


def query(components, *, identifier=None, topic=None):
    return _lookup(components, identifier, topic)[0]


def _pending(diagnosis, mode):
    if mode != "residual":
        return []
    allowed = diagnosis["authority"]["executableServiceIds"]
    if any(not diagnosis["services"][sid]["authorized"] for sid in allowed):
        raise ValueError("Opening knowledge requires consistent OM write authority")
    return sorted(sid for sid in allowed if diagnosis["services"][sid]["status"] in
                  {"observed_pending_admission", "observed_pending_new_demand_revision"})


def _question(work, snapshot_id, state, diagnosis, mode, selected, pending):
    basis = {}
    for sid in pending:
        row = state["services"][sid]
        if (not (row["fresh"] and row["active"] and row["registered"])
                or not 0 <= row["observed_at_ns"] <= row["received_at_ns"] <= state["now_ns"]):
            raise ValueError("Opening knowledge requires a delivered, active demand")
        basis[sid] = {key: row[key] for key in ("revision", "observed_at_ns", "received_at_ns")}
        basis[sid]["status"] = diagnosis["services"][sid]["status"]
    return {"id": "K044", "work": work, "snapshotId": snapshot_id, "timeNs": state["now_ns"],
            "role": "RC", "mode": mode, "selected": selected, "pendingAuthorized": basis}


def _identity(identity):
    reg = identity["knowledge"]
    return {"id": reg["id"], "version": reg["version"], "entrypoint": reg.get("entrypoint", "builtin"),
            "source": reg["source"], "sourceSha256": reg["sha256"],
            "providerSha256": identity.get("providerModuleSha256", {}).get("knowledge")}


def opening_lookup(components, work, snapshot_id, state, diagnosis, mode, selected):
    pending = _pending(diagnosis, mode)
    if not pending:
        return None
    if components is None:
        from component_config import load_components
        components = load_components().activate("knowledge")
    question = _question(work, snapshot_id, state, diagnosis, mode, selected, pending)
    component = _identity(components.identity)
    result, source_hash = _lookup(components, question["id"], None)
    if source_hash != component["sourceSha256"]:
        raise ValueError("Knowledge source changed after component selection")
    return {"schema": "b03.packet.knowledge-io.v1", "input": question,
            "component": component, "output": result}


def check_opening_advice(receipt, before, after, snapshot, authority, admission):
    """Use a matching K044 note to check the residual supplement against the original work order."""
    if not receipt["output"]:
        return {"status": "entry_not_in_edition"}
    if set(before) != set(after):
        raise ValueError("K044 residual check found a different service roster")
    additions = sorted(sid for sid, path in after.items() if path != before[sid])
    if (set(additions) != set(admission["applied"])
            or not set(additions) <= set(authority["executableServiceIds"]) & set(admission["missingAuthorized"])
            or any(before[sid] is not None or after[sid] is None for sid in additions)):
        raise ValueError("K044 residual check found a changed prior path or an unauthorized addition")
    pending = receipt["input"]["pendingAuthorized"]
    checked = {}
    for sid in sorted(set(additions) | set(pending)):
        if after[sid] is not None:
            rate = after[sid]["allocatedBps"]
            if rate != snapshot["requests"][sid]["requirement"]["bandwidthBps"]:
                raise ValueError("K044 residual check found a changed original wire rate")
            if sid in pending:
                checked[sid] = rate
    return {"status": "checked", "retainedPendingIds": sorted(sid for sid in pending if before[sid] is not None),
            "residualAddedIds": additions, "pendingWireBps": checked}


def opening_outcome(receipt, admission, choices, installs):
    """Relate the advice to the original solver, residual search and final device plan."""
    searched = {row["id"] for row in admission["search"]}
    by_service = {}
    for sid in receipt["input"]["pendingAuthorized"]:
        by_service[sid] = {"proposedBySelectedRule": sid not in admission["missingAuthorized"],
                           "residualSearched": sid in searched,
                           "residualFeasible": sid in admission["feasibleAdditions"],
                           "addedByOpening": sid in admission["applied"],
                           "acceptedInFinalPlan": choices[sid]["accepted"],
                           "installRequested": sid in installs}
    return {"handler": "opening.check_opening", "matchedEditionEntry": bool(receipt["output"]),
            "heldBySelectedAction": admission["heldBySelectedAction"], "services": by_service}


def verify_work_order_queries(conditions, events):
    """Replay lookup results and decision evidence from the sealed run, without importing providers."""
    proof = conditions.get("provenance", {})
    required = conditions.get("mode") == "workorder" and "knowledge" in proof.get("activeComponents", {})
    plans = [r for r in events if r["event"] == "b03_rc_plan"]
    if not required:
        if any("knowledge_io" in r for r in plans):
            raise ValueError("Unregistered knowledge receipt")
        return []
    diagnoses = {r["work_id"]: r for r in events if r["event"] == "b03_om_diagnosis"}
    observations = {r["work_id"]: r for r in events if r["event"] == "b03_observation_evidence"
                    and r.get("purpose") == "plan"}
    identity = _identity(conditions["components"])
    edition = proof["knowledge"]
    raw = edition["text"].encode("utf-8")
    if (any(edition[k] != identity[k] for k in ("id", "version"))
            or hashlib.sha256(raw).hexdigest() != identity["sourceSha256"]):
        raise ValueError("Archived knowledge version or source differs")
    wanted = query_library(json.loads(raw), identity["version"], identifier="K044")
    mode = conditions["rcPolicy"]["initialAdmission"]
    first = True
    results = []
    for plan in plans:
        work = plan["work_id"]
        admission = plan.get("solver_audit", {}).get("openingAdmission")
        opening = first and bool(plan.get("real_solver_calls"))
        if opening != (admission is not None):
            raise ValueError("Opening evidence differs from the first authorized solver call")
        if opening:
            first = False
        diagnosis = diagnoses[work]
        pending = _pending(diagnosis, mode) if opening else []
        record = plan.get("knowledge_io")
        if bool(pending) != (record is not None):
            raise ValueError("Opening knowledge query missing or outside its delivered trigger")
        if not pending:
            continue
        observation = observations.get(work)
        if (not observation or diagnosis["snapshot_id"] != plan["snapshot_id"]
                or observation["snapshot_id"] != plan["snapshot_id"]
                or observation["time_ns"] != plan["time_ns"] or diagnosis["time_ns"] != plan["time_ns"]
                or set(plan["authorized"]) != set(diagnosis["authority"]["executableServiceIds"])
                or admission["mode"] != mode):
            raise ValueError("Opening knowledge query differs from its authorized observation")
        question = _question(work, plan["snapshot_id"], observation["state"], diagnosis, mode,
                             plan["tool_choice"]["selected"], pending)
        expected = {"schema": "b03.packet.knowledge-io.v1", "input": question, "component": identity,
                    "output": wanted}
        if wanted:
            retained = sorted(set(pending) - set(admission["missingAuthorized"]))
            added = sorted(admission["applied"])
            if (not set(added) <= set(plan["authorized"]) & set(admission["missingAuthorized"])
                    or not set(added) <= set(admission["feasibleAdditions"]) or added and admission["heldBySelectedAction"]):
                raise ValueError("K044 residual additions disagree with the authorized opening")
            sample = plan.get("component_io")
            if sample and retained != sorted(s for s in pending if sample["returned"]["target"][s] is not None):
                raise ValueError("K044 retained paths differ from the recorded rule proposal")
            checked = {s: plan["decisions"][s]["required_wire_bps"] for s in set(pending) & (set(retained) | set(added))}
            expected["procedureCheck"] = {"status": "checked", "retainedPendingIds": retained,
                                          "residualAddedIds": added, "pendingWireBps": checked}
        else:
            expected["procedureCheck"] = {"status": "entry_not_in_edition"}
        expected["disposition"] = opening_outcome(expected, admission, plan["decisions"], plan["install_ids"])
        if record != expected:
            raise ValueError("Archived knowledge input, source, result or actual disposition differs")
        results.append({"work": work, "id": question["id"], "version": identity["version"],
                        "sourceSha256": identity["sourceSha256"], "pending": pending,
                        "disposition": expected["disposition"]})
    return results
