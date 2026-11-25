"""Optional work-order contexts sourced from a pinned, read-only knowledge edition."""
from copy import deepcopy
from time import perf_counter_ns


def configured(reg):
    bindings = reg.get("bindings", {})
    if not isinstance(bindings, dict) or bindings.keys() - {"om","rules","model"}:
        raise ValueError("Knowledge bindings must name om, rules or model")
    for q in bindings.values():
        if (not isinstance(q, dict) or set(q) not in ({"identifier"},{"topic"})
                or not all(isinstance(v,str) and v.strip() for v in q.values())):
            raise ValueError("Each knowledge binding selects one ID or topic")
    return bindings


def lookup(components, port, work, snap, stamp):
    if not components or port not in configured(components.knowledge):
        return None
    from tech_knowledge import _identity, _lookup
    start = perf_counter_ns()
    question = components.knowledge["bindings"][port]
    component = _identity(components.identity)
    output, sha = _lookup(components, question.get("identifier"), question.get("topic"))
    if component["sourceSha256"] != sha:
        raise ValueError("Knowledge edition changed after selection")
    return {"schema":"b03.knowledge.context.v2", "work":work, "snapshotId":snap, "timeNs":stamp,
            "port":port, "query":deepcopy(question), "component":component, "output":output,
            "wallNs":perf_counter_ns()-start}


def accepts_model(model, policy):
    return (policy.get("kind") == "model-adapter" and model.get("entrypoint") != "builtin"
            and model.get("options",{}).get("acceptKnowledge") is True)


def accepts_rules(reg, symbol):
    opts = reg.get("options", {})
    return (reg.get("contractVersion") == "b03.rules.patch.v2" and opts.get("acceptKnowledge") is True
            and (symbol in opts.get("customTools", {}) or symbol in opts.get("providedNative", [])))


def verify(conditions, events, entries, identity):
    from strong_tools import digest
    bindings = configured(conditions["components"]["knowledge"])
    registered = conditions["components"]["rules"]
    proof = []
    diagnoses = {r["work_id"]:r for r in events if r["event"]=="b03_om_diagnosis"}
    states = {r["work_id"]:r for r in events if r["event"]=="b03_observation_evidence" and r.get("purpose")=="plan"}
    for plan in (r for r in events if r["event"]=="b03_rc_plan"):
        work, snap = plan["work_id"], plan["snapshot_id"]
        records = plan.get("knowledge_contexts",{})
        choice = plan["tool_choice"]
        rules_port = plan["real_solver_calls"] and accepts_rules(registered, choice["selected"])
        model_port = choice["modelCalled"] and accepts_model(conditions["components"]["model"],conditions["rcPolicy"])
        enabled = ({"om"} | ({"rules"} if rules_port else set()) | ({"model"} if model_port else set())) & bindings.keys()
        if not isinstance(records,dict) or set(records) != enabled:
            raise ValueError("A knowledge port receipt is missing or outside its work-order trigger")
        diagnosis = diagnoses[work]
        observation = choice.get("observation",{})
        for port in sorted(enabled):
            rec = records[port]
            if (not isinstance(rec,dict) or set(rec) != {"schema","work","snapshotId","timeNs","port","query","component","output","wallNs"}
                    or rec["schema"] != "b03.knowledge.context.v2" or rec["work"] != work or rec["port"] != port
                    or rec["snapshotId"] != snap or diagnosis["snapshot_id"] != snap
                    or rec["timeNs"] != plan["time_ns"] or rec["query"] != bindings[port]
                    or rec["component"] != identity or type(rec["wallNs"]) is not int or rec["wallNs"] < 0
                    or work not in states or states[work]["snapshot_id"] != snap or digest(states[work]["state"]) != snap):
                raise ValueError("Knowledge context differs from the sealed observation, query or edition")
            rows = rec["output"]
            if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
                raise ValueError("Invalid knowledge result")
            ids = [r.get("id") for r in rows]
            if (not all(isinstance(k,str) for k in ids) or len(set(ids)) != len(ids)
                    or any(r != entries.get(k) for k,r in zip(ids,rows))):
                raise ValueError("Ranked results contain duplicates or differ from the pinned edition")
            key = rec["query"].get("identifier")
            if key is not None and ids != ([key] if key in entries else []):
                raise ValueError("Exact knowledge lookup changed the identifier")
            proof.append({"work":work,"port":port,"rankedIds":ids,"version":identity["version"],"wallNs":rec["wallNs"]})
        def match_slot(target, port, delivered):
            if ("knowledgeContext" in target) != delivered or delivered and target["knowledgeContext"] != records[port]["output"]:
                raise ValueError("Consumer context differs from delivered knowledge")
        match_slot(diagnosis, "om", "om" in enabled)
        match_slot(observation, "model", "model" in enabled)
        patch = plan.get("solver_audit",{}).get("rulePatch")
        cfg = registered.get("options", {})
        plugin_tools = set(cfg.get("providedNative",[])) | set(cfg.get("customTools",{}))
        expected_patch = (registered.get("contractVersion") == "b03.rules.patch.v2" and bool(plan["real_solver_calls"])
                          and choice.get("selected") in plugin_tools)
        if bool(patch) != expected_patch:
            raise ValueError("Missing or unexpected rule patch receipt")
        if patch:
            from rule_component import verify_patch_receipt
            verify_patch_receipt(conditions, registered, plan, diagnosis, states[work], patch, records.get("rules",{}).get("output"))
    return proof
