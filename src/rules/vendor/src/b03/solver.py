"""Budgeted deterministic paths and allocation, using only current visible state.

No reference paths, postEvents, simulator truth, or future events are accepted.
QoS is an explicit flow-level development model, not packet simulation.
"""
from __future__ import annotations

import copy
import b03_formulas as formula
import heapq
import math

QOS = ('delayMs', 'jitterMs', 'ber', 'packetLossRate')
EPS = formula.EPS


def active_requests(requests, now):
    return {sid: r for sid, r in requests.items()
            if not r.get('_deleted', False) and r.get('startAtSec', 0) <= now
            and (r.get('endAtSec') is None or now < r['endAtSec'])}


def usable(nodes, link):
    return (link.get('_online', True) and link['params']['bandwidthBps'] > 0
            and nodes.get(link['srcNodeId'], {}).get('_online', False)
            and nodes.get(link['dstNodeId'], {}).get('_online', False))


def path_metrics(links, path):
    return formula.path_metrics([links[lid]['params'] for lid in path['linkIds']])


def path_errors(nodes, links, request, path):
    ns, ls = path.get('nodes', []), path.get('linkIds', [])
    if not ns or len(ns) != len(ls)+1 or ns[0] != request['srcNodeId'] or ns[-1] != request['dstNodeId']:
        return ['PATH_ENDPOINT_OR_LENGTH']
    if len(set(ns)) != len(ns):
        return ['PATH_NOT_SIMPLE']
    if any(n not in nodes or not nodes[n].get('_online', False) for n in ns):
        return ['NODE_OFFLINE_OR_UNKNOWN']
    for a, b, lid in zip(ns, ns[1:], ls):
        if lid not in links or {a,b} != {links[lid]['srcNodeId'],links[lid]['dstNodeId']}:
            return ['PATH_LINK_MISMATCH']
        if not usable(nodes, links[lid]):
            return ['LINK_UNAVAILABLE']
    metrics = path_metrics(links, path)
    return ['QOS_'+k for k in QOS if metrics[k] > request['requirement'][k]+EPS]


def loads(links, assignments):
    result = dict.fromkeys(links, 0)
    for a in assignments.values():
        if a is not None:
            for lid in a['linkIds']:
                if lid in result:
                    result[lid] += a['allocatedBps']
    return result


def assess(nodes, links, requests, assignments, now):
    active = active_requests(requests, now)
    load = loads(links, assignments)
    caps = {lid: l['params']['bandwidthBps'] if usable(nodes,l) else 0 for lid,l in links.items()}
    overload = {lid for lid in links if not formula.within_capacity(load[lid], caps[lid], EPS)}
    details = {}
    for sid, req in sorted(active.items()):
        a = assignments.get(sid)
        errors = ['UNADMITTED'] if a is None else path_errors(nodes, links, req, a)
        if a is not None:
            if a.get('allocatedBps',0) + EPS < req['requirement']['bandwidthBps']:
                errors.append('BANDWIDTH_BELOW_ORIGINAL_REQUIREMENT')
            errors += ['CAPACITY_'+lid for lid in a['linkIds'] if lid in overload]
        details[sid] = {'serviceId':sid, 'priority':req['priority'], 'satisfied':not errors,
                        'assigned':a is not None, 'reasons':errors,
                        'category':req.get('category'), 'serviceType':req.get('serviceType')}
    adjacency = {n:set() for n in nodes}
    for link in links.values():
        if usable(nodes,link):
            a,b=link['srcNodeId'],link['dstNodeId']; adjacency[a].add(b); adjacency[b].add(a)
    reach = {}
    for n in nodes:
        seen = {n} if nodes[n].get('_online',False) else set()
        pending = list(seen)
        while pending:
            for other in adjacency[pending.pop()] - seen:
                seen.add(other); pending.append(other)
        reach[n] = seen
    online = [n for n in nodes if nodes[n].get('_online',False)]
    pair_den = formula.online_pair_count(len(online))
    reachable = sum(r['dstNodeId'] in reach.get(r['srcNodeId'],set()) for r in active.values())
    good = [sid for sid,d in details.items() if d['satisfied']]
    total_weight = sum(r['priority'] for r in active.values())
    return {'requestCount':len(active), 'satisfiedCount':len(good),
            'satisfiedFraction':len(good)/len(active) if active else None,
            'priorityWeightTotal':total_weight,
            'satisfiedPriorityWeight':sum(active[s]['priority'] for s in good),
            'onlineNodeCount':len(online), 'onlineNodePairDenominator':pair_den,
            'onlineNodePairConnectedFraction':formula.ratio(sum(len(reach[n])-1 for n in online),pair_den),
            'originalActiveEndpointDenominator':len(active),
            'originalActiveEndpointReachableCount':reachable,
            'originalActiveEndpointReachableFraction':formula.ratio(reachable,len(active)),
            'overloadedLinkIds':sorted(overload), 'linkLoadsBps':load,
            'services':details}


def candidate_paths(nodes, links, request, config):
    """Uniform-cost bounded simple paths; parallel edges retain their linkIds."""
    src,dst=request['srcNodeId'],request['dstNodeId']
    if src not in nodes or dst not in nodes or not nodes[src].get('_online',False) or not nodes[dst].get('_online',False):
        return [], {'expanded':0,'termination':'endpoint_unavailable','complete':True}
    adjacency={n:[] for n in nodes}
    for lid,l in sorted(links.items()):
        if usable(nodes,l) and formula.within_capacity(request['requirement']['bandwidthBps'], l['params']['bandwidthBps'], EPS):
            a,b=l['srcNodeId'],l['dstNodeId']; adjacency[a].append((b,lid)); adjacency[b].append((a,lid))
    heap=[(0.0,(),(src,))]; found=[]; expanded=0
    hop_limit=config.get('max_path_hops') or max(0,len(nodes)-1)
    while heap and expanded<config['path_expansion_budget'] and len(found)<config['path_limit']:
        delay,lids,ns=heapq.heappop(heap); expanded+=1
        if ns[-1]==dst:
            found.append({'nodes':list(ns),'linkIds':list(lids),'allocatedBps':request['requirement']['bandwidthBps']}); continue
        if len(lids)>=hop_limit: continue
        for other,lid in adjacency[ns[-1]]:
            if other in ns: continue
            path={'nodes':list(ns+(other,)),'linkIds':list(lids+(lid,))}
            metrics=path_metrics(links,path)
            if any(metrics[k]>request['requirement'][k]+EPS for k in QOS): continue
            heapq.heappush(heap,(metrics['delayMs'],lids+(lid,),ns+(other,)))
    reason='candidate_limit' if len(found)>=config['path_limit'] and heap else ('expansion_budget' if heap else 'exhausted')
    return found, {'expanded':expanded,'termination':reason,'complete':not heap and hop_limit>=len(nodes)-1}


def objective(requests, target, current):
    return formula.priority_objective(requests, target, current)


def solve(nodes, links, requests, current, now, config, method='priority_greedy', initialize=False, *, writable_service_ids=None):
    active=active_requests(requests,now)
    current_eval=assess(nodes,links,requests,current,now)
    target={sid:None for sid in requests}
    healthy={sid for sid,d in current_eval['services'].items() if d['satisfied']}
    protected=set(config['protected_service_ids'])
    frozen=set() if initialize else healthy | (protected & set(active))
    if writable_service_ids is not None:
        # OM can see requests whose device configuration is awaiting a fresh readback.
        # Their wire reservations already count in the ledger; only this work order's IDs may change.
        frozen.update(set(requests) - set(writable_service_ids))
    for sid in frozen: target[sid]=copy.deepcopy(current.get(sid))
    work=sorted((sid for sid in active if sid not in frozen),key=lambda sid:(-active[sid]['priority'],sid))
    pool={}; path_audit={}
    for sid in work:
        pool[sid],path_audit[sid]=candidate_paths(nodes,links,active[sid],config)
    load=loads(links,target)
    capacity={lid:l['params']['bandwidthBps'] if usable(nodes,l) else 0 for lid,l in links.items()}
    def fits(path,occupancy):
        return all(formula.within_capacity(occupancy[lid]+path['allocatedBps'], capacity[lid], EPS) for lid in path['linkIds'])
    def add(path,occupancy,sign):
        for lid in path['linkIds']: occupancy[lid]+=sign*path['allocatedBps']
    for sid in work:
        for path in pool[sid]:
            if fits(path,load): target[sid]=path; add(path,load,1); break
    explored=0; search_exhausted=False
    if method=='bounded_search' and not initialize:
        best=copy.deepcopy(target); best_key=objective(active,best,current)
        trial={sid:copy.deepcopy(current.get(sid)) if sid in frozen else None for sid in requests}
        occupancy=loads(links,trial)
        # Limit recursion independently of graph size; otherwise retain greedy.
        search_work=work[:config.get('search_service_limit',12)]
        for sid in work[len(search_work):]:
            trial[sid]=copy.deepcopy(target[sid])
            if trial[sid] is not None: add(trial[sid],occupancy,1)
        def dfs(i):
            nonlocal explored,best,best_key,search_exhausted
            if explored>=config['search_budget']:
                search_exhausted=True; return
            explored+=1
            if i==len(search_work):
                key=objective(active,trial,current)
                if key>best_key: best,best_key=copy.deepcopy(trial),key
                return
            sid=search_work[i]
            for path in pool[sid]:
                if fits(path,occupancy):
                    trial[sid]=path; add(path,occupancy,1); dfs(i+1); add(path,occupancy,-1)
                    if search_exhausted: break
            trial[sid]=None
            if not search_exhausted: dfs(i+1)
        dfs(0); target=best
    audit={'pathSearch':path_audit,'searchStates':explored,
           'searchTermination':'budget_exhausted' if search_exhausted else 'bounded_scope_completed',
           'frozenHealthyServiceIds':sorted(healthy),'protectedServiceIds':sorted(protected),
           'scope':'failed_and_unadmitted_active_requests; healthy assignments frozen',
           'globallyOptimal':False, 'physicalInfeasibilityProven':False,
           'unadmittedReasons':{sid:'NO_FEASIBLE_ALLOCATION_IN_SEARCHED_SCOPE' for sid in active if target.get(sid) is None}}
    if writable_service_ids is not None:
        for sid in active.keys() - set(writable_service_ids):
            if sid in audit['unadmittedReasons']:
                audit['unadmittedReasons'][sid] = 'OUTSIDE_WORK_ORDER_WRITE_SCOPE'
    return target,audit
