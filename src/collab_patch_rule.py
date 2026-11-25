"""A standalone rule: route writable services in priority order using delivered links."""
from collections import Counter, defaultdict
from heapq import heappop, heappush
import b03_formulas as formula


class Router:
    def enumerate_actions(self, request):
        return [row["id"] for row in request["candidates"] if row["tool"] == "ranked_feasible_path"]

    def solve_action(self, request):
        snapshot, limits = request["snapshot"], request["budgets"]
        routes = snapshot["assignments"]
        occupied = Counter()
        superseded = request.get("conditionalReservations", {})
        for route in [*routes.values(), *superseded.values()]:
            if route:
                for link in route["linkIds"]:
                    occupied[link] += route["allocatedBps"]
        neighbors = defaultdict(list)
        for lid, link in sorted(snapshot["links"].items()):
            a, b = link["srcNodeId"], link["dstNodeId"]
            if link["_online"] and snapshot["nodes"][a]["_online"] and snapshot["nodes"][b]["_online"]:
                neighbors[a].append((b, lid, link["params"]))
                neighbors[b].append((a, lid, link["params"]))
        order = "hops"
        used = []
        for row in request.get("knowledgeContext", []):
            if row["id"] in {"EX.PATH.DELAY", "EX.PATH.HOPS"}:
                order = "delay" if row["id"] == "EX.PATH.DELAY" else "hops"
                used = [row["id"]]
                break
        patch, expanded = {}, 0
        requests = snapshot["requests"]
        priority_order = sorted(request["action"]["scope"]["serviceIds"], key=lambda sid:(-requests[sid]["priority"],sid))
        for sid in priority_order:
            if len(patch) >= limits["maxChangedServices"] or expanded >= limits["search_budget"]:
                break
            old = routes[sid] or superseded.get(sid)
            if old:
                for lid in old["linkIds"]:
                    occupied[lid] -= old["allocatedBps"]
            best, cost = find_path(requests[sid], neighbors, occupied, limits["maxHops"], order,
                                   min(limits["path_expansion_budget"], limits["search_budget"]-expanded))
            expanded += cost
            if best and best != routes[sid]:
                patch[sid] = best
            keep = best or old
            if keep:
                for lid in keep["linkIds"]:
                    occupied[lid] += keep["allocatedBps"]
        return {"patch":patch, "audit":{"orderBy":order, "usedKnowledge":used, "expanded":expanded}}


def find_path(service, neighbors, occupied, hop_limit, order, budget):
    req = service["requirement"]
    rate = req["bandwidthBps"]
    start, goal = service["srcNodeId"], service["dstNodeId"]
    queue = [((0, 0), (start,), (), 0., 0., 0., 1.)]
    expanded = 0
    while queue and expanded < budget:
        _, nodes, edges, delay, jitter_sq, bit_error, delivery = heappop(queue)
        expanded += 1
        if nodes[-1] == goal and edges:
            return {"nodes":list(nodes),"linkIds":list(edges),"allocatedBps":rate}, expanded
        if len(edges) >= hop_limit:
            continue
        for peer,lid,link in neighbors[nodes[-1]]:
            if peer in nodes or not formula.within_capacity(occupied[lid] + rate, link["bandwidthBps"]):
                continue
            d, j2, ber, delivered = formula.extend_path(delay, jitter_sq, bit_error, delivery, link)
            if formula.exceeds_qos((d, j2, ber, delivered), req):
                continue
            hops = len(edges)+1
            key = (d,hops) if order=="delay" else (hops,d)
            heappush(queue,(key, nodes+(peer,), edges+(lid,), d,j2,ber,delivered))
    return None, expanded


def build_rules(*, config):
    return Router()
