"""Controller checks for replaceable packet rules. Components produce proposals from detached data."""
from copy import deepcopy
from time import perf_counter_ns

import strong_tools
from b03.solver import active_requests

BASE_CATALOG = "b03.tools.v1"
DEFAULT_RULES = {"id": "b03.strong-tools", "version": "1.0.0", "entrypoint": "builtin",
                 "catalogVersion": BASE_CATALOG}
NATIVE_IDS = {spec[0] for spec in strong_tools.SPECS}


def binding(action, bound):
    """Commit an external return to this work order, snapshot, authority, action and budgets."""
    return {**bound["identity"], "configSha256": strong_tools.digest(bound["config"]),
            "actionSha256": strong_tools.digest(action)}


def bind_proposal(action, bound, target, audit):
    """Finish a proposal with controller-verifiable identities, without changing the target."""
    return target, {**audit, "binding": binding(action, bound), "targetHash": strong_tools.digest(target)}


def descriptor(chooser):
    components = getattr(chooser, "components", None)
    return DEFAULT_RULES if components is None else components.rules


def external_rules(chooser):
    spec = descriptor(chooser)
    if spec["entrypoint"] == "builtin":
        return None
    rules = getattr(chooser, "_packet_external_rules", None)
    if rules is None:
        components = chooser.components
        implementation = components.factory("rules")(config=deepcopy(spec))
        rules = CheckedRules(implementation, spec, components.symbols, chooser.context)
        chooser._packet_external_rules = rules
    return rules


def begin_sample(chooser, action, catalog, bound, context):
    spec = descriptor(chooser)
    return deepcopy({"schema": "b03.packet.rule-io.v1",
                     "component": {k: spec[k] for k in ("id", "version", "entrypoint", "catalogVersion")},
                     "catalog": catalog, "action": action, "bound": bound, "context": context})


class CheckedRules:
    """One local plugin and private solver context per chooser."""
    def __init__(self, implementation, spec, symbols, context=None):
        if not all(callable(getattr(implementation, name, None)) for name in ("enumerate_actions", "solve_action")):
            raise ValueError("Rules must implement enumerate_actions and solve_action")
        self.implementation = implementation
        self.context = deepcopy(context) if context is not None else {}
        self.symbols = tuple(symbols)
        self.aliases = dict(spec.get("options", {}).get("toolAliases", {}))
        if (set(self.aliases) & NATIVE_IDS or set(self.aliases) - set(self.symbols)
                or set(self.aliases.values()) - NATIVE_IDS or set(self.symbols) - NATIVE_IDS - set(self.aliases)
                or (self.aliases and spec["catalogVersion"] == BASE_CATALOG)):
            raise ValueError("An extra tool needs a registered symbol, native template and new catalog version")

    def _legal(self, snapshot, authority, config):
        native = {a["id"]: a for a in strong_tools.enumerate_actions(snapshot, authority, config)}
        legal = {}
        for symbol in self.symbols:
            template = self.aliases.get(symbol, symbol)
            if template in native:
                legal[symbol] = {**native[template], "id": symbol}
        return legal

    def catalog(self, bound, timing):
        before = perf_counter_ns()
        snapshot, authority, config = bound["snapshot"], bound["authority"], bound["config"]
        legal = self._legal(snapshot, authority, config)
        args = deepcopy((snapshot, authority, config, self.context))
        called = perf_counter_ns()
        supplied = self.implementation.enumerate_actions(*args)
        returned = perf_counter_ns()
        if not isinstance(supplied, list) or not supplied:
            raise ValueError("Rules returned an empty or invalid action catalog")
        selected = []
        for row in supplied:
            if not isinstance(row, dict) or type(row.get("id")) is not str or row != legal.get(row["id"]):
                raise ValueError("Catalog action differs from its authorized method, scope or budget")
            selected.append(row["id"])
        if len(set(selected)) != len(selected):
            raise ValueError("Duplicate rule catalog symbol")
        # Return clean controller-owned rows in registered order.
        result = [legal[symbol] for symbol in self.symbols if symbol in selected]
        timing["catalogHookWallNs"] = returned - called
        timing["catalogGuardWallNs"] = called - before + perf_counter_ns() - returned
        return result

    def solve(self, action, offered, bound, timing):
        before = perf_counter_ns()
        snapshot, authority, config = bound["snapshot"], bound["authority"], bound["config"]
        ident = bound["identity"]
        if (ident["physicalStateHash"] != strong_tools.digest(snapshot)
                or ident["authorityHash"] != strong_tools.digest(authority)
                or action.get("id") not in offered or self._legal(snapshot, authority, config).get(action["id"]) != action):
            raise ValueError("Selected action or bound work-order snapshot changed")
        args = deepcopy((action, bound))
        called = perf_counter_ns()
        value = self.implementation.solve_action(*args, context=self.context)
        returned = perf_counter_ns()
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("Rules must return (target, audit)")
        target, raw = deepcopy(value)
        if not isinstance(target, dict) or set(target) != set(snapshot["requests"]):
            raise ValueError("Proposal must contain the complete original service roster")
        if (not isinstance(raw, dict) or raw.get("binding") != binding(action, bound)
                or raw.get("targetHash") != strong_tools.digest(target)):
            raise ValueError("Proposal target or original work-order binding differs")
        current = snapshot["assignments"]
        active = active_requests(snapshot["requests"], snapshot["observedAtSec"])
        writable = set(authority["executableServiceIds"])
        frozen = set(authority["frozenHealthyServiceIds"]) | set(authority["protectedServiceIds"])
        changes = {sid for sid, path in target.items() if current[sid] != path}
        permitted = set(action["scope"]["serviceIds"])
        if action["tool"] != "hold_current":
            permitted.update(s for s in writable if s not in active and target[s] is None)
        if changes - (writable & permitted) or changes & frozen:
            raise ValueError("Proposal changes held, protected or out-of-scope services")
        for sid in changes:
            path = target[sid]
            if path is not None and (not isinstance(path, dict) or set(path) != {"nodes", "linkIds", "allocatedBps"}
                                     or not isinstance(path["nodes"], list) or not isinstance(path["linkIds"], list)
                                     or type(path["allocatedBps"]) not in (int, float)
                                     or path["allocatedBps"] <= 0):
                raise ValueError("Invalid proposed service assignment")
            hop_limit = config.get("max_path_hops")
            if path is not None and hop_limit is not None and len(path["linkIds"]) > hop_limit:
                raise ValueError("Proposed route exceeds the work-order hop budget")
        _check_budget(action, raw)
        guard = strong_tools.validate_proposal(snapshot, config, authority, target)
        if not guard["valid"]:
            raise ValueError("Original proposal guard rejected component output: " + ", ".join(guard["errors"]))
        checked = {**raw, **guard, "action": deepcopy(action), "permissionVersion": authority["policyVersion"],
                   "sourceSnapshotHash": ident["physicalStateHash"], "targetHash": raw["targetHash"],
                   "healthyFrozen": True}
        timing["solverHookWallNs"] = returned - called
        timing["proposalGuardWallNs"] = called - before + perf_counter_ns() - returned
        return target, raw, checked


def _check_budget(action, audit):
    limits = strong_tools.BUDGETS.get(action["budget"], {})
    if "budgetLimits" in audit and audit["budgetLimits"] != limits:
        raise ValueError("Proposal changed the selected tool budget")
    for field, limit in (("localEvaluations", "local_evaluations"), ("passes", "local_passes"),
                         ("mipNodeCount", "milp_nodes")):
        count = audit.get(field)
        if limit in limits and count is not None and (type(count) not in (int, float) or not 0 <= count <= limits[limit]):
            raise ValueError("Reported rule search exceeded " + limit)
    for row in audit.get("pathSearch", {}).values():
        for field, limit in (("expanded", "path_expansion_budget"), ("selectedCandidates", "path_limit")):
            if limit in limits and field in row and (type(row[field]) is not int or not 0 <= row[field] <= limits[limit]):
                raise ValueError("Reported path search exceeded " + limit)
