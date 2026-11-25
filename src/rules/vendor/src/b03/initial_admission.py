"""Top up the original priority-greedy deployment on its remaining capacity."""
from __future__ import annotations

import heapq
import itertools
import math
import time
from collections import Counter

import b03_formulas as formula
from .solver import EPS, active_requests, candidate_paths, loads, path_errors, usable


POLICY = 'b03.initial.residual-admission.v2'
DEFAULTS = {
    'initial_admission_mode': 'residual',
    'initial_admission_expansion_budget': None,
    'initial_admission_executable_ids': None,
}


def validate_settings(config):
    if config['initial_admission_mode'] not in ('legacy', 'residual'):
        raise ValueError('UNKNOWN_INITIAL_ADMISSION_MODE')
    budget = config['initial_admission_expansion_budget']
    if budget is not None and (type(budget) is not int or budget < 1):
        raise ValueError('POSITIVE_INITIAL_ADMISSION_EXPANSION_BUDGET_REQUIRED')
    allow = config['initial_admission_executable_ids']
    if allow is not None and (not isinstance(allow, list)
                              or any(not isinstance(sid, str) or not sid for sid in allow)
                              or len(set(allow)) != len(allow)):
        raise ValueError('INVALID_INITIAL_ADMISSION_EXECUTABLE_IDS')


def residual_links(available, load, request):
    """Capacity uses the same addition and tolerance as the native allocation guard."""
    demand = request['requirement']['bandwidthBps']
    return {lid: link for lid, link in available.items()
            if formula.within_capacity(load[lid] + demand, link['params']['bandwidthBps'], EPS)}


def reachable(source, destination, edges):
    if source == destination:
        return True, 1
    adjacency = {}
    for link in edges.values():
        a, b = link['srcNodeId'], link['dstNodeId']
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    seen = {source}
    stack = [source]
    while stack:
        for other in adjacency.get(stack.pop(), ()):
            if other == destination:
                return True, None
            if other not in seen:
                seen.add(other)
                stack.append(other)
    return False, len(seen)


def qos_limits(request, n_nodes):
    return formula.search_limits(request["requirement"], n_nodes)


def lower_bounds(adjacency, destination, metric):
    distance = {destination: 0.}
    queue = [(0., destination)]
    settled = 0
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != distance[node]:
            continue
        settled += 1
        for other, _, vector in adjacency[node]:
            value = cost + vector[metric]
            if value < distance.get(other, math.inf):
                distance[other] = value
                heapq.heappush(queue, (value, other))
    return distance, settled


def guided_search(nodes, edges, request, config):
    """Best-first *simple* paths with four admissible remaining-cost bounds.

    Labels carry their visited nodes, so merging unlike histories cannot discard
    the only feasible suffix. An exhausted expansion/hop budget is inconclusive.
    """
    src, dst = request['srcNodeId'], request['dstNodeId']
    budget = config['path_expansion_budget']
    max_hops = min(config['max_path_hops'] or len(nodes) - 1, len(nodes) - 1)
    limits = qos_limits(request, len(nodes))
    adjacency = {node: [] for node in nodes}
    for lid, link in sorted(edges.items()):
        a, b = link['srcNodeId'], link['dstNodeId']
        p = link['params']
        vector = formula.search_vector(p)
        adjacency[a].append((b, lid, vector))
        adjacency[b].append((a, lid, vector))
    audit = {'method': 'reverse_qos_simple_path_astar', 'expansionBudget': budget,
             'expanded': 0, 'reverseSettled': 0, 'prunedByBounds': 0}
    metrics = [i for i in range(4) if math.isfinite(limits[i])]
    bounds = {}
    for i in metrics:
        distance, settled = lower_bounds(adjacency, dst, i)
        bounds[i] = distance
        audit['reverseSettled'] += settled
        if distance.get(src, math.inf) > limits[i]:
            audit['lowerBoundMetric'] = ('delayMs', 'jitterSquared', 'ber', 'negativeLogSurvival')[i]
            return None, 'infeasible', 'shortest_metric_lower_bound', audit
    order = itertools.count()
    start = (0., 0., 0., 0.)
    delay_bound = bounds.get(0, {})
    h0 = delay_bound.get(src, 0.)
    # Small remaining delay breaks ties in equal-cost contours without opening
    # thousands of all-prefix routes before reaching the first destination.
    queue = [(h0, h0, next(order), start, (src,), ())]
    hit_hops = False
    near_boundary = False
    while queue:
        if audit['expanded'] >= budget:
            return None, 'undetermined', 'expansion_budget', audit
        _, _, _, vector, chain, lids = heapq.heappop(queue)
        audit['expanded'] += 1
        node = chain[-1]
        if node == dst:
            path = {'nodes': list(chain), 'linkIds': list(lids),
                    'allocatedBps': request['requirement']['bandwidthBps']}
            if not path_errors(nodes, edges, request, path):
                return path, 'added', 'guided_residual_path', audit
            near_boundary = True
            continue
        if len(lids) >= max_hops:
            hit_hops = hit_hops or max_hops < len(nodes) - 1
            continue
        for other, lid, cost in adjacency[node]:
            if other in chain:
                continue
            next_cost = tuple(a + b for a, b in zip(vector, cost))
            if any(next_cost[i] + bounds[i].get(other, math.inf) > limits[i] for i in metrics):
                audit['prunedByBounds'] += 1
                continue
            h = delay_bound.get(other, 0.)
            heapq.heappush(queue, (next_cost[0] + h, h, next(order), next_cost,
                                   chain + (other,), lids + (lid,)))
    if near_boundary or hit_hops:
        return None, 'undetermined', 'native_numeric_boundary' if near_boundary else 'hop_limit', audit
    return None, 'infeasible', 'all_guided_residual_paths_exhausted', audit


def find_path(nodes, available, load, request, config):
    src, dst = request['srcNodeId'], request['dstNodeId']
    if not (nodes[src]['_online'] and nodes[dst]['_online']):
        return None, {'outcome': 'infeasible', 'reason': 'endpoint_offline', 'expanded': 0}
    edges = residual_links(available, load, request)
    connected, count = reachable(src, dst, edges)
    if not connected:
        return None, {'outcome': 'infeasible', 'reason': 'residual_capacity_cut',
                      'reachableNodeCount': count, 'expanded': 0}
    paths, audit = candidate_paths(nodes, edges, request, config)
    if paths:
        return paths[0], {'outcome': 'added', 'reason': 'feasible_residual_path',
                          'expanded': audit['expanded']}
    if audit['complete']:
        return None, {'outcome': 'infeasible', 'reason': 'original_qos_search_exhausted',
                      'expanded': audit['expanded']}
    path, outcome, reason, fallback = guided_search(nodes, edges, request, config)
    fallback['trigger'] = 'expansion_budget' if audit['termination'] == 'expansion_budget' else 'hop_limit'
    return path, {'outcome': outcome, 'reason': reason,
                  'expanded': audit['expanded'] + fallback['expanded'],
                  'nativeExpanded': audit['expanded'], 'fallback': fallback}


def supplement(nodes, links, requests, initial, config):
    """Only use time-zero facts. Previously installed paths keep their complete allocation."""
    start = time.perf_counter()
    cpu = time.process_time()
    active = active_requests(requests, 0)
    missing = sorted((sid for sid in active if initial[sid] is None),
                     key=lambda sid: (-active[sid]['priority'], sid))
    mode = config['initial_admission_mode']
    allowed = config['initial_admission_executable_ids']
    if allowed is not None and not set(allowed) <= requests.keys():
        raise ValueError('UNKNOWN_INITIAL_ADMISSION_EXECUTABLE_ID')
    limit = config['initial_admission_expansion_budget'] or config['path_expansion_budget']
    baseline = len(active) - len(missing)
    record = {'policy': POLICY, 'mode': mode, 'baselineSatisfiedCount': baseline,
              'finalSatisfiedCount': baseline, 'baselineUnadmittedServiceIds': missing,
              'addedServiceIds': [], 'rows': [], 'outcomes': {}, 'wallSeconds': 0., 'cpuSeconds': 0.,
              'expansionBudgetPerRequest': limit, 'maxPathHops': config['max_path_hops'],
              'fallbackExpansionBudgetPerRequest': limit, 'fallbackCalls': 0, 'fallbackExpanded': 0,
              'resourceScope': 'time_zero_active_requests_current_residual_with_installed_paths_fixed',
              'initializationInsideBusinessClock': False}
    if mode == 'legacy':
        return initial, record
    if not missing:
        record['wallSeconds'] = time.perf_counter() - start
        record['cpuSeconds'] = time.process_time() - cpu
        return initial, record
    target = initial.copy()
    load = loads(links, target)
    available = {lid: link for lid, link in links.items() if usable(nodes, link)}
    writable = set(active) if allowed is None else set(allowed)
    protected = set(config['protected_service_ids'])
    search = dict(config, path_limit=1, path_expansion_budget=limit)
    for sid in missing:
        req = active[sid]
        row = {'serviceId': sid, 'priority': req['priority'],
               'requiredBps': req['requirement']['bandwidthBps']}
        record['rows'].append(row)
        if sid in protected or sid not in writable:
            row.update(outcome='protected', expanded=0,
                       reason='protected_service' if sid in protected else 'outside_execution_scope')
            continue
        path, detail = find_path(nodes, available, load, req, search)
        row.update(detail)
        if 'fallback' in detail:
            record['fallbackCalls'] += 1
            record['fallbackExpanded'] += detail['fallback']['expanded']
        if path is None:
            continue
        demand = req['requirement']['bandwidthBps']
        if path['allocatedBps'] != demand or path_errors(nodes, links, req, path):
            raise RuntimeError('INITIAL_ADMISSION_PATH_GUARD')
        spare = [links[lid]['params']['bandwidthBps'] - load[lid] for lid in path['linkIds']]
        for lid in path['linkIds']:
            if not formula.within_capacity(load[lid] + demand, links[lid]['params']['bandwidthBps'], EPS):
                raise RuntimeError('INITIAL_ADMISSION_CAPACITY_GUARD')
        for lid in path['linkIds']:
            load[lid] += demand
        target[sid] = path
        row.update(path=path, minimumResidualBeforeBps=min(spare, default=None))
        record['addedServiceIds'].append(sid)
    record['finalSatisfiedCount'] += len(record['addedServiceIds'])
    record['outcomes'] = dict(Counter(r['outcome'] for r in record['rows']))
    record['wallSeconds'] = time.perf_counter() - start
    record['cpuSeconds'] = time.process_time() - cpu
    return target, record
