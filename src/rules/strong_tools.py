"""T096 classic recovery tools. Pure proposals; Runtime remains the only writer.
Imports the frozen/current b03 solver from caller's sys.path. No future events.
"""
from __future__ import annotations
import copy, hashlib, itertools, json, time, warnings
from types import SimpleNamespace
from b03.solver import (active_requests, assess, candidate_paths, loads, objective,
                        path_errors, path_metrics, solve, usable, EPS)
from b03.engine import Runtime

VERSION = "t096.strong-tools.v1"
BUDGETS = {
    "small": {"path_limit": 4, "path_expansion_budget": 2000, "pool_scan_limit": 16,
              "local_evaluations": 600, "local_passes": 1, "milp_seconds": 0.10,
              "milp_nodes": 1000},
    "medium": {"path_limit": 8, "path_expansion_budget": 8000, "pool_scan_limit": 32,
               "local_evaluations": 4000, "local_passes": 3, "milp_seconds": 0.50,
               "milp_nodes": 10000}}
SPECS = [
    ("A", "hold_current", "current", "none", "none", "retain"),
    ("B", "priority_greedy", "authorized_failed", "historical", "historical", "repair"),
    ("C", "bounded_search", "authorized_failed", "historical", "historical", "repair"),
    ("D", "backup_restore", "authorized_failed", "cached_low_overlap", "small", "repair"),
    ("E", "pair_local", "authorized_failed_pairs", "shortest", "small", "repair"),
    ("F", "pair_local", "authorized_failed_pairs", "low_overlap", "small", "repair"),
    ("G", "path_milp", "authorized_failed", "shortest", "small", "repair"),
    ("H", "path_milp", "authorized_failed", "capacity_aware", "small", "repair"),
    ("I", "pair_local", "authorized_failed_pairs", "low_overlap", "medium", "repair"),
    ("J", "path_milp", "authorized_failed", "capacity_aware", "medium", "repair"),
    ("K", "pool_greedy", "authorized_failed", "capacity_aware", "small", "repair"),
    ("L", "pool_greedy", "authorized_failed", "low_overlap", "medium", "repair")]

def clone(x): return copy.deepcopy(x)
def digest(x): return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def enumerate_actions(snapshot, authority, config, history=None):
    """Stable catalog. IDs are encoding only; semantic fields are authoritative.
    Scope uses original service IDs, never hidden/future state or entity indices.
    """
    writable = sorted(set(authority["executableServiceIds"]) &
                      set(active_requests(snapshot["requests"], snapshot["observedAtSec"])))
    allowed = set(authority["allowedMethods"])
    return [{"id": i, "tool": tool, "scope": {"kind": scope, "serviceIds": [] if i == "A" else writable},
             "pool": pool, "budget": budget, "stage": stage}
            for i, tool, scope, pool, budget, stage in SPECS if tool in allowed]

def default_authority(snapshot, config):
    a = assess(snapshot["nodes"], snapshot["links"], snapshot["requests"],
               snapshot["assignments"], snapshot["observedAtSec"])
    healthy = sorted(k for k, v in a["services"].items() if v["satisfied"])
    protected = sorted(config["protected_service_ids"])
    return {"policyId": VERSION, "policyVersion": "healthy-frozen.v1",
            "readableServiceIds": sorted(snapshot["requests"]),
            "executableServiceIds": sorted(set(snapshot["requests"]) - set(healthy) - set(protected)),
            "frozenHealthyServiceIds": healthy, "protectedServiceIds": protected,
            "allowedMethods": sorted({z[1] for z in SPECS})}

def _setup(s, authority):
    current = s["assignments"]; active = active_requests(s["requests"], s["observedAtSec"])
    writable = set(authority["executableServiceIds"])
    if writable & (set(authority["frozenHealthyServiceIds"]) | set(authority["protectedServiceIds"])):
        raise ValueError("FROZEN_WRITE_AUTHORIZATION")
    evaluation = assess(s["nodes"], s["links"], s["requests"], current, s["observedAtSec"])
    healthy = {k for k, v in evaluation["services"].items() if v["satisfied"]}
    if writable & healthy:
        raise ValueError("V1_REQUIRES_COMMON_HEALTHY_FREEZE")
    base = {sid: clone(a) if sid not in writable else None for sid, a in current.items()}
    work = sorted(writable & set(active), key=lambda k: (-active[k]["priority"], k))
    caps = {lid: l["params"]["bandwidthBps"] if usable(s["nodes"], l) else 0 for lid, l in s["links"].items()}
    # Frozen invalid assignments may already exceed physical capacity. Never worsen.
    cap_guard = {lid: max(cap, loads(s["links"], base)[lid]) for lid, cap in caps.items()}
    return active, base, work, cap_guard

def _fits(path, occupancy, caps):
    return all(occupancy[lid] + path["allocatedBps"] <= caps[lid] + EPS for lid in path["linkIds"])

def _add(path, occupancy, sign):
    if path:
        for lid in path["linkIds"]: occupancy[lid] += sign * path["allocatedBps"]

def generate_pool(s, work, base, config, kind, budget):
    """Candidate generation only. Capacity-aware ranking is not an allocator."""
    params = dict(config, path_limit=budget["pool_scan_limit"],
                  path_expansion_budget=budget["path_expansion_budget"])
    occupancy = loads(s["links"], base); pools = {}; audit = {}
    for sid in work:
        paths, a = candidate_paths(s["nodes"], s["links"], s["requests"][sid], params)
        current = s["assignments"].get(sid)
        if current and not path_errors(s["nodes"], s["links"], s["requests"][sid], current) and current not in paths:
            paths.append(clone(current))
        def delay(p): return (path_metrics(s["links"], p)["delayMs"], tuple(p["linkIds"]))
        if kind == "capacity_aware":
            def rank(p):
                ratios = [(occupancy[lid]+p["allocatedBps"])/s["links"][lid]["params"]["bandwidthBps"] for lid in p["linkIds"]]
                return (max(ratios, default=0), *delay(p))
            paths.sort(key=rank)
        elif kind == "low_overlap":
            chosen = []; remaining = sorted(paths, key=delay)
            while remaining:
                used = set(x for p in chosen for x in p["linkIds"])
                p = min(remaining, key=lambda p: (len(set(p["linkIds"]) & used), *delay(p)))
                chosen.append(p); remaining.remove(p)
            paths = chosen
        else: paths.sort(key=delay)
        pools[sid] = paths[:budget["path_limit"]]
        audit[sid] = dict(a, scannedCandidates=len(paths), selectedCandidates=len(pools[sid]),
                         generator=kind, finitePool=True)
    return pools, audit

def _greedy(base, work, pools, caps, links):
    target = clone(base); occupancy = loads(links, target)
    for sid in work:
        for path in pools[sid]:
            if _fits(path, occupancy, caps):
                target[sid] = clone(path); _add(path, occupancy, 1); break
    return target

def prepare_backups(snapshot, config, context=None, budget_name="small"):
    """Explicit preparation/maintenance on one visible snapshot. No reservation.
    Caller must charge this measured preparation and preserve cache across events.
    """
    context = {} if context is None else context
    w = time.perf_counter(); c = time.process_time()
    active = active_requests(snapshot["requests"], snapshot["observedAtSec"])
    pool, audit = generate_pool(snapshot, sorted(active), {}, config, "low_overlap", BUDGETS[budget_name])
    entries = {sid: {"requestHash": digest(snapshot["requests"][sid]), "paths": paths}
               for sid, paths in pool.items()}
    cache = {"schema": VERSION + ".backup", "preparedSnapshotHash": digest(snapshot),
             "preparedAtSec": snapshot["observedAtSec"], "entries": entries,
             "reservationBps": 0, "pathSearch": audit}
    cost = {"wallSeconds": time.perf_counter()-w, "cpuSeconds": time.process_time()-c,
            "candidateCount": sum(len(z["paths"]) for z in entries.values()),
            "cacheBytes": len(json.dumps(cache).encode()), "reservationBps": 0}
    cache["preparationCost"] = cost; context["backupCache"] = cache
    return cost

def _backup(s, config, context, base, work, caps):
    cold = "backupCache" not in context
    cost = prepare_backups(s, config, context) if cold else None
    cache = context["backupCache"]; pools = {}; rejected = {}
    for sid in work:
        entry = cache["entries"].get(sid); pools[sid] = []; reasons = []
        if entry is None: reasons.append("NOT_PRECOMPUTED")
        elif entry["requestHash"] != digest(s["requests"][sid]): reasons.append("REQUEST_CHANGED")
        else:
            for p in entry["paths"]:
                errors = path_errors(s["nodes"], s["links"], s["requests"][sid], p)
                if errors: reasons.append(errors)
                elif p["allocatedBps"] == s["requests"][sid]["requirement"]["bandwidthBps"]:
                    pools[sid].append(clone(p))
        rejected[sid] = reasons
    target = _greedy(base, work, pools, caps, s["links"])
    return target, {"solverStatus": "cached_backup_checked", "coldPreparation": cold,
                    "preparationChargedInsideCall": cost, "cachedPreparationCost": cache["preparationCost"],
                    "preparedAtSec": cache["preparedAtSec"], "preparedSnapshotHash": cache["preparedSnapshotHash"],
                    "cacheRejected": rejected, "onlineCandidateGeneration": cold,
                    "reservationBps": 0, "candidateCounts": {k: len(v) for k,v in pools.items()},
                    "noOnlineFallback": True}

def _local(s, active, base, work, pools, caps, budget):
    best = _greedy(base, work, pools, caps, s["links"]); key = objective(active, best, s["assignments"])
    count = 0; improvements = 0; exhausted = False
    search_work = [sid for sid in work if pools[sid]]
    # Empty pools cannot participate in a reroute; do not spend pair budget there.
    neighborhoods = list(itertools.combinations(search_work, 2)) + [(sid,) for sid in search_work]
    neighborhoods.sort(key=lambda group: (all(best[sid] is not None for sid in group), -len(group), group))
    for pass_id in range(budget["local_passes"]):
        improved = False
        for group in neighborhoods:
            trial = clone(best)
            for sid in group: trial[sid] = None
            occupancy = loads(s["links"], trial)
            for choices in itertools.product(*[pools[sid]+[None] for sid in group]):
                if count >= budget["local_evaluations"]:
                    exhausted = True; break
                count += 1; check = dict(occupancy); feasible = True
                for p in choices:
                    if p and not _fits(p, check, caps): feasible = False; break
                    _add(p, check, 1)
                if not feasible: continue
                for sid, p in zip(group, choices): trial[sid] = p
                candidate_key = objective(active, trial, s["assignments"])
                within_budget = sum(trial[k] != s["assignments"][k] for k in trial) <= budget.get("max_changed_services", len(trial))
                if candidate_key > key and within_budget:
                    best = clone(trial); key = candidate_key; improvements += 1; improved = True
            if exhausted: break
        if exhausted or not improved: break
    return best, {"solverStatus": "budget_exhausted" if exhausted else "bounded_pair_scope_completed",
                  "localEvaluations": count, "acceptedImprovements": improvements,
                  "maxJointNeighborhood": 2, "passes": pass_id+1, "finitePoolOptimal": False}

def _milp(s, active, base, work, pools, caps, budget):
    # SciPy exposes no x0 / MIP-start. Keep a checked greedy incumbent externally.
    import numpy as np
    import scipy
    from scipy.optimize import Bounds, LinearConstraint, milp
    incumbent = _greedy(base, work, pools, caps, s["links"])
    variables = [(sid, p) for sid in work for p in pools[sid]]
    if not variables:
        return incumbent, {"solverStatus": "empty_candidate_pool", "finitePoolOptimal": True, "variableCount": 0}
    # Lexicographic priority counts over the decisions available in this pool.
    # Each higher-priority unit dominates every lower-priority unit and change cost.
    from collections import Counter
    mutable = {sid for sid, _ in variables}
    counts = Counter(active[sid]["priority"] for sid in mutable)
    unit = len(mutable) + 1
    weights = {}
    for priority, count in sorted(counts.items()):
        weights[priority] = unit
        unit *= count + 1
    if max(weights.values(), default=0) * max(1, len(variables)) > 2**50:
        return incumbent, {"solverStatus": "objective_scaling_refused", "finitePoolOptimal": False}
    # Counts are lexicographic, then fewer changed service assignments (historical objective).
    costs = []
    for sid, p in variables:
        cur = s["assignments"].get(sid)
        change_delta = 1 if cur is None else (-1 if cur == p else 0)
        costs.append(-weights[active[sid]["priority"]] + change_delta)
    rows = []; lower = []; upper = []
    for sid in work:
        rows.append([float(k == sid) for k,p in variables]); lower.append(0); upper.append(1)
    occupied = loads(s["links"], base)
    # Normalize bandwidth rows for HiGHS numeric stability; exact original guard rechecks.
    for lid in s["links"]:
        scale = max(1., caps[lid])
        rows.append([p["allocatedBps"]/scale if lid in p["linkIds"] else 0. for sid,p in variables])
        lower.append(-np.inf); upper.append((caps[lid]-occupied[lid])/scale)
    # Enforce the same explicit changed-service ceiling checked after every tool.
    max_changes = int(budget.get("max_changed_services", len(s["assignments"])))
    constant_changes = sum(base[sid] != s["assignments"][sid] for sid in base)
    rows.append([1. if s["assignments"][sid] is None else (-1. if s["assignments"][sid] == p else 0.)
                 for sid,p in variables])
    lower.append(-np.inf); upper.append(max_changes-constant_changes)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = milp(np.array(costs), integrality=np.ones(len(variables)),
                  bounds=Bounds(np.zeros(len(variables)), np.ones(len(variables))),
                  constraints=LinearConstraint(np.array(rows), np.array(lower), np.array(upper)),
                  options={"time_limit": budget["milp_seconds"], "node_limit": budget["milp_nodes"],
                           "mip_rel_gap": 0.0, "presolve": True, "threads": 1})
    feasible = False
    incumbent_in_budget = sum(incumbent[k] != s["assignments"][k] for k in incumbent) <= max_changes
    target = incumbent if incumbent_in_budget else clone(s["assignments"])
    if result.x is not None and all(abs(float(x)-round(float(x))) <= 1e-5 for x in result.x):
        proposal = clone(base); duplicate = False
        for x,(sid,p) in zip(result.x, variables):
            if x > .5:
                if proposal[sid] is not None: duplicate = True
                proposal[sid] = clone(p)
        load = loads(s["links"], proposal)
        feasible = not duplicate and all(load[lid] <= caps[lid]+EPS for lid in caps)
        if feasible and (not incumbent_in_budget or objective(active, proposal, s["assignments"]) >= objective(active, incumbent, s["assignments"])):
            target = proposal
    return target, {"solverStatus": {0:"optimal_in_finite_pool",1:"limit_reached",2:"infeasible_in_finite_pool",3:"unbounded",4:"solver_error"}.get(result.status,"unknown"),
                    "solverMessage": str(result.message), "solverLibrary": "scipy.optimize.milp/HiGHS",
                    "scipyVersion": scipy.__version__, "variableCount": len(variables),
                    "solverWarnings": [str(w.message) for w in caught],
                    "highsThreadsRequested": 1, "maxChangedServices": max_changes,
                    "constraintCount": len(rows), "returnedIntegerFeasible": feasible,
                    "finitePoolOptimal": result.status == 0 and feasible,
                    "mipGap": float(result.mip_gap) if getattr(result,"mip_gap",None) is not None else None,
                    "mipNodeCount": int(result.mip_node_count) if getattr(result,"mip_node_count",None) is not None else None,
                    "incumbent": "external_greedy_fallback; SciPy API has no MIP start",
                    "timeLimitSeconds": budget["milp_seconds"], "nodeLimit": budget["milp_nodes"]}

def plan_actions(snapshot, target):
    changed = sorted(k for k in target if target[k] != snapshot["assignments"][k])
    return ([{"action":"release","serviceId":k,"assignment":None} for k in changed if snapshot["assignments"][k] is not None]
            + [{"action":"install","serviceId":k,"assignment":clone(target[k])} for k in changed if target[k] is not None])

def validate_proposal(snapshot, config, authority, target):
    s = snapshot; changed = sorted(k for k in target if target[k] != s["assignments"].get(k))
    errors = []
    if set(target) != set(s["requests"]): errors.append("TARGET_SERVICE_SET")
    if not set(changed) <= set(authority["executableServiceIds"]): errors.append("CANDIDATE_OUTSIDE_AUTHORIZATION")
    limit = config.get("t096_max_changed_services", len(authority["executableServiceIds"]))
    if type(limit) is not int or limit < 0: raise ValueError("INVALID_CHANGED_SERVICE_BUDGET")
    if len(changed) > limit: errors.append("CHANGED_SERVICE_BUDGET_EXCEEDED")
    rt = SimpleNamespace(nodes=s["nodes"],links=s["links"],requests=s["requests"],assignments=s["assignments"],
                         now=s["observedAtSec"],config=config,state_version=s["stateVersion"],
                         config_version=s["configVersion"],objective_version=s["objectiveVersion"])
    plan = {"baseStateVersion": s["stateVersion"], "baseConfigVersion":s["configVersion"],
            "objectiveVersion":s["objectiveVersion"],"target":target}
    errors += Runtime.validate_plan(rt, plan)
    trial = clone(s["assignments"]); prefixes = []
    before_eval = assess(s["nodes"],s["links"],s["requests"],trial,s["observedAtSec"])
    before_healthy = {k for k,v in before_eval["services"].items() if v["satisfied"]}
    actions = plan_actions(s, target)
    for i,action in enumerate(actions):
        before = loads(s["links"], trial); trial[action["serviceId"]] = clone(action["assignment"])
        after = assess(s["nodes"],s["links"],s["requests"],trial,s["observedAtSec"])
        violated = [lid for lid,l in s["links"].items()
                    if after["linkLoadsBps"][lid] > max(before[lid], l["params"]["bandwidthBps"] if usable(s["nodes"],l) else 0)+EPS]
        if violated: errors.append("PREFIX_NEW_OR_WORSE_OVERLOAD:" + ",".join(violated))
        prefixes.append({"index":i,"action":action["action"],"serviceId":action["serviceId"],
                         "satisfiedCount":after["satisfiedCount"],"satisfiedPriorityWeight":after["satisfiedPriorityWeight"],
                         "unservedServiceIds": sorted(k for k,v in after["services"].items() if not v["satisfied"]),
                         "initiallyHealthyInterrupted":sorted(k for k in before_healthy if not after["services"][k]["satisfied"]),
                         "overloadedLinkIds":after["overloadedLinkIds"],"newOrWorseOverload":violated})
    return {"valid":not errors, "errors":errors, "changedServiceIds":changed,
            "proposedActions":actions, "prefixRisk":prefixes,
            "executionOrder":"all_releases_then_installs; same as original Driver",
            "physicalGuardStillRequired": True, "configurationWriter": False}

def solve_action(action, bound, context=None):
    w = time.perf_counter(); c = time.process_time()
    s = bound["snapshot"]; config = bound["config"]; authority = bound["authority"]
    context = {} if context is None else context
    legal = enumerate_actions(s, authority, config, context)
    if action not in legal: raise ValueError("ILLEGAL_OR_STALE_STRUCTURED_ACTION")
    active, base, work, caps = _setup(s, authority)
    tool = action["tool"]; audit = {}
    if tool == "hold_current": target = clone(s["assignments"]); audit["solverStatus"] = "not_run"
    elif tool in {"priority_greedy", "bounded_search"}:
        target, audit = solve(s["nodes"], s["links"], s["requests"], s["assignments"],
                              s["observedAtSec"], config, tool, writable_service_ids=authority["executableServiceIds"])
        audit["solverStatus"] = "historical_implementation"
    elif tool == "backup_restore":
        target,audit = _backup(s,config,context,base,work,caps)
    else:
        budget = dict(BUDGETS[action["budget"]],
                      max_changed_services=config.get("t096_max_changed_services", len(authority["executableServiceIds"])))
        pool, path_audit = generate_pool(s,work,base,config,action["pool"],budget)
        if tool == "pool_greedy":
            target = _greedy(base,work,pool,caps,s["links"])
            audit = {"solverStatus":"greedy_in_finite_pool", "allocator":"sequential_priority_greedy",
                     "candidateGeneratorSeparate":True, "finitePoolOptimal":False}
        elif tool == "pair_local": target,audit = _local(s,active,base,work,pool,caps,budget)
        elif tool == "path_milp": target,audit = _milp(s,active,base,work,pool,caps,budget)
        else: raise ValueError("UNKNOWN_TOOL")
        audit["pathSearch"] = path_audit
    check = validate_proposal(s, config, authority, target)
    if not check["valid"]:
        audit["rejectedCandidate"] = check
        target = clone(s["assignments"])
        check = validate_proposal(s, config, authority, target)
        audit["solverStatus"] = "proposal_rejected_fallback_hold"
    after = assess(s["nodes"],s["links"],s["requests"],target,s["observedAtSec"])
    audit.update(check)
    audit.update(schema=VERSION,action=clone(action),budgetLimits=clone(BUDGETS.get(action["budget"],{})),
                 permissionVersion=authority["policyVersion"],healthyFrozen=True,
                 solverWallSeconds=time.perf_counter()-w, solverCpuSeconds=time.process_time()-c,
                 maxChangedServices=config.get("t096_max_changed_services", len(authority["executableServiceIds"])),
                 satisfiedCount=after["satisfiedCount"],satisfiedPriorityWeight=after["satisfiedPriorityWeight"],
                 targetHash=digest(target),globallyOptimal=False,physicalInfeasibilityProven=False,
                 sourceSnapshotHash=digest(s),contextCachePresent="backupCache" in context)
    return target, audit

def choose_nonlearning(actions, snapshot):
    """Current-state conditional scheduler; no counterfactual/oracle solver calls."""
    indexed = {a["id"]:a for a in actions}
    metrics = assess(snapshot["nodes"],snapshot["links"],snapshot["requests"],snapshot["assignments"],snapshot["observedAtSec"])
    failed = metrics["requestCount"]-metrics["satisfiedCount"]
    desired = "A" if failed == 0 else "D" if failed <= 2 else "E" if failed <= 6 else "H"
    return indexed.get(desired, indexed.get("B", actions[0]))
