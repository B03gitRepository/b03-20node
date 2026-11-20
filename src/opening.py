"""Bounded time-of-first-work-order admission on the delivered residual network."""
from b03.initial_admission import find_path
from b03.solver import active_requests, loads, path_errors, usable


def check_opening(target, snapshot, config, authority, mode, hold):
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
    for sid in missing:
        req=active[sid]
        path,detail=find_path(nodes,available,occupancy,req,config)
        audit["search"].append({"id":sid,**detail})
        if path is None:
            continue
        if path["allocatedBps"]!=req["requirement"]["bandwidthBps"] or path_errors(nodes,links,req,path):
            raise ValueError("Residual path violates the original service contract")
        for lid in path["linkIds"]:
            after=occupancy[lid]+path["allocatedBps"]
            if after>links[lid]["params"]["bandwidthBps"]+1e-9:
                raise ValueError("Residual allocation exceeds available wire capacity")
            occupancy[lid]=after
        topped[sid]=path
        audit["feasibleAdditions"].append(sid)
    if mode=="residual" and not hold:
        audit["applied"]=list(audit["feasibleAdditions"])
        return topped,audit
    return target,audit
