"""Pure fixed-rule example. Search delivered residual links by bottleneck headroom and delay."""
from copy import deepcopy
from heapq import heappop, heappush

from b03.solver import EPS, QOS, active_requests, loads, usable
import strong_tools
from rule_component import bind_proposal, custom_catalog


def _route(request, neighbors, occupied, budget, hop_limit):
    demand = request["requirement"]
    rate = demand["bandwidthBps"]
    start, goal = request["srcNodeId"], request["dstNodeId"]
    queue = [(-float("inf"), 0.0, (), (start,), 0.0, 0.0, 1.0)] if start in neighbors and goal in neighbors else []
    expanded = 0
    while queue and expanded < budget["path_expansion_budget"]:
        neg_free, delay, edges, nodes, jitter_sq, ber, delivery = heappop(queue)
        expanded += 1
        if nodes[-1] == goal:
            return {"nodes": list(nodes), "linkIds": list(edges), "allocatedBps": rate}, expanded
        if len(edges) >= hop_limit:
            continue
        for other, lid, params in neighbors[nodes[-1]]:
            free = params["bandwidthBps"] - occupied[lid] - rate
            if other in nodes or free < -EPS:
                continue
            path_delay = delay + params["delayMs"]
            path_jitter_sq = jitter_sq + params["jitterMs"] ** 2
            path_ber = min(1.0, ber + params["ber"])
            path_delivery = delivery * (1.0 - params["packetLossRate"])
            values = (path_delay, path_jitter_sq ** .5, path_ber, 1.0 - path_delivery)
            if any(v > demand[k] + EPS for k, v in zip(QOS, values)):
                continue
            heappush(queue, (max(neg_free, -free), path_delay, edges + (lid,), nodes + (other,),
                             path_jitter_sq, path_ber, path_delivery))
    return None, expanded


class ResidualBranchRules:
    def __init__(self, config):
        self.tools = deepcopy(config["options"]["customTools"])

    def enumerate_actions(self, snapshot, authority, config, history=None):
        native = strong_tools.enumerate_actions(snapshot, authority, config)
        return custom_catalog(native, self.tools)

    def solve_action(self, action, bound, context=None):
        if action["tool"] != "widest_residual" or action["pool"] != "delivered_neighbors":
            raise ValueError("This provider implements only the registered residual search")
        snapshot, authority, config = bound["snapshot"], bound["authority"], bound["config"]
        budget = dict(strong_tools.BUDGETS[action["budget"]])
        active = active_requests(snapshot["requests"], snapshot["observedAtSec"])
        nodes, links = snapshot["nodes"], snapshot["links"]
        neighbors = {n: [] for n, row in nodes.items() if row.get("_online", False)}
        for lid, link in sorted(links.items()):
            if usable(nodes, link):
                a, b = link["srcNodeId"], link["dstNodeId"]
                neighbors[a].append((b, lid, link["params"]))
                neighbors[b].append((a, lid, link["params"]))
        order = sorted(action["scope"]["serviceIds"], key=lambda s: (-active[s]["priority"], s))
        target = deepcopy(snapshot["assignments"])
        for sid in set(authority["executableServiceIds"]) - active.keys():
            target[sid] = None
        occupied = loads(links, target)
        search = {}
        hop_limit = config.get("max_path_hops")
        if hop_limit is None:
            hop_limit = len(nodes) - 1
        for sid in order:
            old = target[sid]
            if old is not None:
                for lid in old["linkIds"]:
                    occupied[lid] -= old["allocatedBps"]
            path, expanded = _route(active[sid], neighbors, occupied, budget, hop_limit)
            target[sid] = path
            if path is not None:
                for lid in path["linkIds"]:
                    occupied[lid] += path["allocatedBps"]
            search[sid] = {"expanded": expanded, "selectedCandidates": int(path is not None)}
        max_changes = config.get("t096_max_changed_services", len(authority["executableServiceIds"]))
        changed = sum(target[s] != old for s, old in snapshot["assignments"].items())
        status = "bounded_residual_search"
        if changed > max_changes:
            target = deepcopy(snapshot["assignments"])
            status = "change_budget_hold"
        return bind_proposal(action, bound, target,
                             {"solverStatus": status, "allocator": "widest_residual", "serviceOrder": order,
                              "pathSearch": search, "budgetLimits": budget, "maxChangedServices": max_changes,
                              "globallyOptimal": False})


def build_rules(*, config):
    return ResidualBranchRules(config)
