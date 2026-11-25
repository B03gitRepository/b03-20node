"""Host translation for the plain-data rule patch contract."""
from copy import deepcopy
import json
import math

V2 = "b03.rules.patch.v2"


def registrations(spec, symbols, native_ids, native_methods):
    opts = spec.get("options", {})
    custom = opts.get("customTools", {})
    if not isinstance(custom, dict) or set(custom) != set(symbols) - native_ids:
        raise ValueError("Register every new rule ID under its versioned catalog")
    implemented = opts.get("providedNative", [])
    if (not isinstance(implemented, list) or len(set(implemented)) != len(implemented)
            or set(implemented) - (set(symbols) & native_ids)):
        raise ValueError("providedNative must name registered native symbols")
    if (custom or implemented) and spec["catalogVersion"] == "b03.tools.v1":
        raise ValueError("Changing a tool implementation requires a separately versioned catalog")
    budgets = opts.get("outputBudgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("outputBudgets must map names to output limits")
    for label, limits in budgets.items():
        if (not isinstance(label, str) or not label.strip() or not isinstance(limits, dict)
                or set(limits) - {"maxChangedServices", "maxHops", "maxSolverWallMs"}
                or any(type(v) is not int or v < 0 for v in limits.values())):
            raise ValueError("Invalid v2 output budget")
    methods = set(native_methods)
    for reg in custom.values():
        if (not isinstance(reg, dict) or set(reg) != {"tool","scope","permit","budget","pool"}
                or not all(isinstance(v, str) and v.strip() for v in reg.values())
                or reg["scope"] not in ("writable_active", "retain") or reg["permit"] not in native_methods
                or reg["tool"] in methods or reg["budget"] not in ({"workorder"} | set(budgets))):
            raise ValueError("Invalid v2 rule registration or permission")
        methods.add(reg["tool"])
    return deepcopy(custom), set(implemented), deepcopy(budgets)


def catalog(custom, native, snapshot, authority):
    from b03.solver import active_requests
    active = active_requests(snapshot["requests"], snapshot["observedAtSec"])
    writable = sorted(set(authority["executableServiceIds"]) & active.keys())
    offered = dict(native)
    for symbol, reg in custom.items():
        if reg["permit"] in authority["allowedMethods"]:
            retain = reg["scope"] == "retain"
            offered[symbol] = {"id":symbol, "tool":reg["tool"], "stage":"retain" if retain else "repair",
                               "scope":{"kind":reg["scope"], "serviceIds":[] if retain else writable},
                               "pool":reg["pool"], "budget":reg["budget"]}
    return offered


def limits(config, authority, snapshot, extensions):
    writable = len(authority["executableServiceIds"])
    hops = config.get("max_path_hops")
    if hops is None:
        hops = max(0, len(snapshot["nodes"])-1)
    return {**({"maxSolverWallMs":extensions["maxSolverWallMs"]} if "maxSolverWallMs" in extensions else {}),
            **{k: config[k] for k in ("path_limit","path_expansion_budget","search_budget","search_service_limit") if k in config},
            "maxHops":min(hops, extensions.get("maxHops", hops)),
            "maxChangedServices":min(config.get("t096_max_changed_services", writable),
                                     extensions.get("maxChangedServices",writable), writable)}


def request(bound, *, candidates=None, action=None, budget=None):
    msg = {"schema":V2, "work":bound["identity"]["workOrderId"], "snapshot":bound["snapshot"],
           "authority":bound["authority"], "budgets":limits(bound["config"], bound["authority"],
                                                           bound["snapshot"], budget or {})}
    if action is not None:
        msg["action"] = action
        if "knowledgeContext" in bound:
            msg["knowledgeContext"] = bound["knowledgeContext"]
    else:
        msg["candidates"] = candidates
    return deepcopy(msg)


def materialize(msg, reply):
    if (not isinstance(reply, dict) or not {"patch"} <= reply.keys() <= {"patch","audit"}
            or not isinstance(reply["patch"], dict) or not isinstance(reply.get("audit", {}),dict)):
        raise ValueError("Rules v2 must return a patch and optional audit object")
    json.dumps(reply, allow_nan=False)
    observed = msg["snapshot"]
    patch = reply["patch"]
    allowed = set(msg["action"]["scope"]["serviceIds"]) & set(msg["authority"]["executableServiceIds"])
    frozen = set(msg["authority"]["protectedServiceIds"]) | set(msg["authority"]["frozenHealthyServiceIds"])
    if patch.keys() - (allowed - frozen) or patch.keys() - observed["assignments"].keys():
        raise ValueError("Rule patch touches a frozen or unauthorized service")
    budget = msg["budgets"]
    for sid,path in patch.items():
        if path is None:
            continue
        if (not isinstance(path,dict) or set(path) != {"nodes","linkIds","allocatedBps"}
                or not isinstance(path["nodes"],list) or not isinstance(path["linkIds"],list)
                or not all(isinstance(x,str) for x in path["nodes"] + path["linkIds"])
                or not path["linkIds"] or len(set(path["nodes"])) != len(path["nodes"])
                or len(path["nodes"]) != len(path["linkIds"])+1 or len(path["linkIds"]) > budget["maxHops"]
                or type(path["allocatedBps"]) not in (float,int) or not math.isfinite(path["allocatedBps"])
                or path["allocatedBps"] != observed["requests"][sid]["requirement"]["bandwidthBps"]):
            raise ValueError("Rule patch changes the rate or violates the route or hop limit")
    target = {**deepcopy(observed["assignments"]), **deepcopy(patch)}
    if sum(target[s] != old for s,old in observed["assignments"].items()) > budget["maxChangedServices"]:
        raise ValueError("Rule patch exceeds the change budget")
    audit = reply.get("audit", {})
    used = audit.get("usedKnowledge", [])
    valid_ids = {row["id"] for row in msg.get("knowledgeContext", [])}
    if (not isinstance(used,list) or not all(isinstance(k,str) for k in used)
            or len(set(used)) != len(used) or set(used) - valid_ids):
        raise ValueError("Rule audit cites knowledge outside its delivered context")
    return target
