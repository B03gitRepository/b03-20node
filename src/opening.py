"""Bounded time-of-first-work-order admission on the delivered residual network."""
from b03.initial_admission import find_path
from b03.solver import active_requests, loads, path_errors, usable


def check_opening(target, snapshot, config, authority, mode, hold, *, rule_request=None):
    active = active_requests(snapshot["requests"],snapshot["observedAtSec"])
    missing = [sid for sid in sorted(active,key=lambda s:(-active[s]["priority"],s))
               if sid in authority["executableServiceIds"] and target.get(sid) is None]
    audit = {"mode":mode, "timeSec":snapshot["observedAtSec"],"missingAuthorized":missing,
             "search":[],"feasibleAdditions":[],"applied":[],"heldBySelectedAction":bool(hold),
             "scope":"first delivered active work order; fixed assignments retained"}
    if mode=="original" or not missing:
        return target,audit
    nodes,links=snapshot["nodes"],snapshot["links"]
    available={k:v for k,v in links.items() if usable(nodes,v)}
    occupancy=loads(links,target)
    topped=dict(target)
    if rule_request is not None:
        budget = rule_request["budgets"]
        permitted = set(rule_request["action"]["scope"]["serviceIds"])
        config = dict(config, max_path_hops=budget["maxHops"])
        changed = sum(v != snapshot["assignments"][sid] for sid, v in target.items())
    for sid in missing:
        if rule_request is not None:
            if hold or sid not in permitted or budget["maxHops"] == 0 or (
                    changed >= budget["maxChangedServices"] and topped[sid] == snapshot["assignments"][sid]):
                audit["search"].append({"id":sid,"outcome":"deferred","reason":"selected_action_scope_or_output_budget","expanded":0})
                continue
        req=active[sid]
        path,detail=find_path(nodes,available,occupancy,req,config)
        audit["search"].append({"id":sid,**detail})
        if path is None:
            continue
        if rule_request is not None and len(path["linkIds"]) > budget["maxHops"]:
            raise ValueError("Residual search exceeded the selected rule hop limit")
        if path["allocatedBps"]!=req["requirement"]["bandwidthBps"] or path_errors(nodes,links,req,path):
            raise ValueError("Residual path violates the original service contract")
        for lid in path["linkIds"]:
            after=occupancy[lid]+path["allocatedBps"]
            if after>links[lid]["params"]["bandwidthBps"]+1e-9:
                raise ValueError("Residual allocation exceeds available wire capacity")
            occupancy[lid]=after
        if rule_request is not None:
            old = snapshot["assignments"][sid]
            changed += int(path != old) - int(topped[sid] != old)
        topped[sid]=path
        audit["feasibleAdditions"].append(sid)
    if mode=="residual" and not hold:
        audit["applied"]=list(audit["feasibleAdditions"])
        return topped,audit
    return target,audit
