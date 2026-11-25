"""Verify a saved packet run from native files, without starting the simulator or its agents."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path


def load(path):
    return json.loads(path.read_bytes())


def lines(path):
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for row in handle:
                yield json.loads(row)


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_business_denominators(intervals, business):
    totals = Counter()
    for row in intervals:
        dt = row["end_ns"] - row["start_ns"]
        if dt <= 0:
            raise ValueError("Invalid scoring interval")
        totals["requested"] += dt * len(row["active"])
        totals["weighted"] += dt * sum(row["active"].values())
        totals["unserved"] += row["U_service_ns"]
        for fraction, denominator in (("connectedPairFraction", "onlinePairDenominator"),
                                      ("activeEndpointReachableFraction", "activeEndpointDenominator")):
            value = row[fraction]
            if row[denominator] < 0 or (value is None) != (row[denominator] == 0):
                raise ValueError("Connectivity interval denominator disagrees with its fraction")
            if value is not None:
                if not 0 <= value <= 1:
                    raise ValueError("Invalid reachability fraction")
                totals[fraction] += dt * value
                totals[denominator] += dt
    def ratio(numerator, denominator):
        return totals[numerator] / totals[denominator] if totals[denominator] else None
    expected = {"requestSeconds": totals["requested"] / 1e9,
                "priorityRequestSeconds": totals["weighted"] / 1e9,
                "UOverRequests": ratio("unserved", "requested"),
                "onlinePairConnectedTimeMean": ratio("connectedPairFraction", "onlinePairDenominator"),
                "activeEndpointReachableTimeMean": ratio("activeEndpointReachableFraction", "activeEndpointDenominator")}
    for key, value in expected.items():
        actual = business[key]
        if (actual is None) != (value is None) or (value is not None and not math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError(f"Business interval evidence disagrees with {key}")


def demand_original(row):
    return (row["required_bps"], row["delay_limit_s"] * 1000, row["jitter_limit_s"] * 1000,
            row["ber_limit"], row["loss_limit"])


def sample_graph(endpoints, latest, stamp, ttl):
    graph = {}
    for lid, (a, b) in endpoints.items():
        pair = [latest.get((lid, n)) for n in (a, b)]
        if None in pair:
            continue
        if any(not (v[0]["observed_at_ns"] <= v[1] <= stamp and stamp-v[0]["observed_at_ns"] <= ttl) for v in pair):
            continue
        if not all(v[0]["node_online"] and v[0]["link"]["endpoint_online"] and v[0]["link"]["admin_up"] for v in pair):
            continue
        left,right = [v[0]["link"] for v in pair]
        graph[lid] = {"ends":(a,b),"capacity":min(left["capacity_bps"],right["capacity_bps"]),
                      "delay":max(left["delay_ns"],right["delay_ns"])/1e6,"jitter":max(left["jitter_ns"],right["jitter_ns"])/1e6,
                      "loss":max(left["packet_loss_rate"],right["packet_loss_rate"]),"ber":max(left["ber"],right["ber"])}
    return graph


def compare_qos(graph, decision, req):
    links, nodes = decision["link_path"],decision["node_path"]
    result = []
    if not all(lid in graph for lid in links):
        return ["missing_or_unavailable"], {}
    assert len(nodes)==len(links)+1 and len(set(nodes))==len(nodes)
    parts=[graph[lid] for lid in links]
    for a,b,row in zip(nodes,nodes[1:],parts):
        assert {a,b}==set(row["ends"])
    metrics={"bandwidthBps":min(row["capacity"] for row in parts),"delayMs":sum(x["delay"] for x in parts),
             "jitterMs":math.sqrt(sum(x["jitter"]**2 for x in parts)),"ber":min(1.,sum(x["ber"] for x in parts)),
             "packetLossRate":1-math.prod(1-x["loss"] for x in parts)}
    if metrics["bandwidthBps"]<decision["wire_bps"]:result.append("bandwidthBps")
    for key in ["delayMs","jitterMs","ber","packetLossRate"]:
        if metrics[key]>req[key]+1e-12: result.append(key)
    return result, metrics


def verify(run, *, strict_source=False, components=None, checkpoint=None, tokenizer=None, simulator=None):
    if not __debug__: raise RuntimeError("Verification requires ordinary Python assertions")
    cond,summary=load(run/"CONDITIONS.json"),load(run/"SUMMARY.json")
    from integrity import check_archive
    verification = check_archive(run, cond, strict_source=strict_source, components=components,
                                 checkpoint=checkpoint, tokenizer=tokenizer, simulator=simulator)
    if "competitionMetrics" in summary or (cond.get("simulatorRuntime") and cond["mode"] == "workorder"):
        from competition_metrics import competition_metrics
        if summary.get("competitionMetrics") != competition_metrics(run, summary):
            raise ValueError("Competition metrics differ from the delivered reports and device receipts")
    mode=cond["mode"]; ct=list(lines(run/"controller.jsonl")); acts=list(lines(run/"b03.jsonl"))
    exe=list(lines(run/"executions.jsonl"));routes=list(lines(run/"routes.jsonl"));bus=list(lines(run/"business-intervals.jsonl"))
    from simulator_ports import verify_ports
    verify_ports(run, cond, summary, ct, exe, bus)
    packet=summary["afterDrain"];sc=run/"input";topo=load(sc/"net-topo.json")
    script=cond["script"]
    for key in ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json"):
        assert sha(sc/key)==cond["sourceHashes"][key], key
    assert len(topo["nodes"])==cond["nodeCount"] and len(topo["links"])==cond["linkCount"]
    endpoints={l["linkId"]:(l["srcNodeId"],l["dstNodeId"]) for l in topo["links"]}
    orig={s["serviceId"]:s for s in load(sc/"bg-service.json")}
    for e in load(sc/f"scripts/script-{script}.json"):
        if e["eventType"]=="service_adjust" and e["attrs"].get("op")=="add":
            orig[e["attrs"]["serviceId"]]=e["attrs"]
    defs=cond["officialDefinitions"]
    for sid, d in defs.items():
        assert sid in orig and orig[sid]["priority"]==d["priority"] and orig[sid]["requirement"]==d["requirement"]
    clocks=summary["clock"]; assert bus and bus[0]["start_ns"]==clocks["start_ns"] and bus[-1]["end_ns"]==clocks["end_ns"]
    assert all(x["end_ns"]==y["start_ns"] for x,y in zip(bus,bus[1:]))
    sums=Counter()
    for x in bus:
        dt=x["end_ns"]-x["start_ns"]
        assert x["U_service_ns"]==dt*len(x["unmet"]) and x["W_service_ns"]==dt*sum(x["active"][sid] for sid in x["unmet"])
        wire=Counter()
        for assignment in x["installed"].values():
            for lid in assignment["linkIds"]:
                wire[lid]+=(assignment["allocatedBps"]*1036+999)//1000
        assert dict(wire)==x["activeWireBps"]
        sums.update(U=x["U_service_ns"],W=x["W_service_ns"],requests=dt*len(x["active"]),
                    bit_ns=dt*sum(defs[s]["requirement"]["bandwidthBps"] for s in x["active"]))
    verify_business_denominators(bus, summary["business"])
    for key in ["U","W"]:assert abs(sums[key]/1e9-summary["business"][key])<1e-7
    assert abs(sums["bit_ns"]/1e9-summary["demandBits"])<=len(defs)
    for counts in [summary["endBoundary"],packet]:
        for cls,x in counts.items():assert x["passed"] and x["generated"]==x["delivered"]+x["dropped"]+x["pending"]
    by_id=defaultdict(list); sends=defaultdict(list); acks=defaultdict(list)
    for x in ct:
        if x["event"]=="command_sent":sends[x["command"]["command_id"]].append(x)
        if x["event"]=="ack_received":acks[x["ack"]["command_id"]].append(x)
    for x in exe:
        cmd=x["command"];cid=cmd["command_id"];by_id[cid].append(x)
        assert cmd["action"]["service_id"] in defs and any(y["command"]==cmd and y["time_ns"]<=x["time_ns"] for y in sends[cid])
        if mode=="workorder" and cmd["action"]["op"]!="query":
            write_set=set(cond["authority"]["allowedServiceIds"])-set(cond["authority"].get("protectedServiceIds",[]))
            assert cmd["action"]["service_id"] in write_set
        if x["status"]=="applied":
            assert x["time_ns"]==x["executed_at_ns"] and cmd["base_version"]==x["before_version"]
            assert x["after_version"]==x["before_version"]+(cmd["action"]["op"]!="query")
    assert all(sum(x["status"]=="applied" for x in li)<=1 for li in by_id.values())
    for cid,li in sends.items():assert all(x["command"]==li[0]["command"] for x in li)
    tally=Counter(x["event"] for x in acts)
    reports=[x for x in ct if x["event"] in ("observation","service_observation") and x["result"]["accepted"]]
    probe_index={(v["probe"]["link"]["link_id"],v["probe"]["node_id"],v["probe"]["seq"]):v for v in reports if v["event"]=="observation"}
    demand_index={(v["message"]["service"]["service_id"],v["message"]["seq"]):v for v in reports if v["event"]=="service_observation"}
    first_report={}
    for entry in reports:
        if entry["event"]=="service_observation":
            message=entry["message"]
            if message["registered"]:first_report.setdefault(message["service"]["service_id"],entry["time_ns"])
    visibility={}
    if mode=="workorder" and cond["authority"].get("discovery")=="all_delivered_registered":
        for entry in (v for v in acts if v["event"] in {"b03_om_diagnosis","b03_rc_plan","b03_observation_evidence"}):
            ids=set(entry.get("allowed_ids",[])) | set(entry.get("authorized",[])) | set(entry.get("decisions",{}))
            ids.update(entry.get("state",{}).get("services",{}))
            for sid in ids:
                assert sid in first_report and first_report[sid]<=entry["time_ns"],("future ID",sid,entry["time_ns"])
                visibility.setdefault(sid,entry["time_ns"])
    causal=Counter()
    for y in (z for z in acts if z["event"]=="b03_observation_evidence"):
        state=y["state"]; now=y["time_ns"]
        content=json.dumps(state,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("utf-8")
        assert hashlib.sha256(content).hexdigest()==y["snapshot_id"] and state["now_ns"]==now
        causal[y["purpose"]]+=1
        for lid,row in state["links"].items():
            assert set(row["reports"])==set(endpoints[lid])
            for node,ref in row["reports"].items():
                if ref is None:
                    causal["missingEndpoint"]+=1
                    assert not row["usable"]
                    continue
                actual=probe_index[lid,node,ref["seq"]]
                assert actual["time_ns"]==ref["received_at_ns"] and actual["probe"]["observed_at_ns"]==ref["sampled_at_ns"]<=ref["received_at_ns"]<=now
                causal[ref["status"]+"Endpoint"]+=1
                if ref["status"]=="stale":assert not row["usable"]
        for sid,row in state["services"].items():
            actual=demand_index[sid,row["seq"]]
            assert actual["time_ns"]==row["received_at_ns"] and actual["message"]["observed_at_ns"]==row["observed_at_ns"]<=row["received_at_ns"]<=now
            causal["freshService" if row["fresh"] else "staleService"]+=1
    plans=[x for x in acts if x["event"] in ("single_query_and_route","b03_rc_plan")]
    current={};last_demand={};i=0;good_count=0;missing_samples=0;route_evidence=[];former={}
    ttl=cond["simulation"]["control"]["observation_ttl_ns"]
    for row in plans:
        stamp=row["time_ns"]
        while i<len(reports) and reports[i]["time_ns"]<=stamp:
            y=reports[i];i+=1
            if y["event"]=="observation":
                v=y["probe"];current[v["link"]["link_id"],v["node_id"]]=(v,y["time_ns"])
            else:
                v=y["message"];last_demand[v["service"]["service_id"]]=(v,y["time_ns"])
        graph=sample_graph(endpoints,current,stamp,ttl); missing_samples+=len(graph)<len(endpoints)
        if row["event"]=="single_query_and_route":
            decisions=[row["decision"]]
        else:
            decisions=[row["decisions"][sid] for sid in row["install_ids"]]
            assert set(row["release_ids"]+row["install_ids"])<=set(row["authorized"])<=set(defs)
            for lid,total in row["wire_ledger"]["proposed_wire_bps"].items():
                if lid in graph and any(lid in x["link_path"] for x in decisions):
                    assert total<=math.floor(graph[lid]["capacity"]*(1-cond["simulation"]["control"]["control_headroom_fraction"])+1e-6)
        for choice in decisions:
            if not choice["accepted"]:continue
            sid=choice["service_id"]; req=defs[sid]["requirement"]; d,received=last_demand[sid]
            assert d["observed_at_ns"]<=received<=stamp and stamp-d["observed_at_ns"]<=ttl and d["active"]
            assert demand_original(d["service"])==(req["bandwidthBps"],req["delayMs"],req["jitterMs"],req["ber"],req["packetLossRate"])
            assert choice["reserved_bps"]==req["bandwidthBps"] and choice["wire_bps"]==(req["bandwidthBps"]*1036+999)//1000
            errs,qos=compare_qos(graph,choice,req);assert not errs,(sid,errs)
            if sid not in former or former[sid]["link_path"]!=choice["link_path"]:
                previous=former.get(sid)
                old_errors,old_metrics=compare_qos(graph,previous,req) if previous else ([],{})
                route_evidence.append({"service":sid,"work":row.get("work_id"),"query_ns":stamp,
                    "serviceObservedAt":d["observed_at_ns"],"serviceReceivedAt":received,
                    "previousPath":previous["link_path"] if previous else None,"oldViolations":old_errors,
                    "oldMetrics":old_metrics,"path":choice["link_path"],"nodes":choice["node_path"],"payload":choice["reserved_bps"],
                    "wire":choice["wire_bps"],"qos":qos,"requirement":req,
                    "pathReportClocks":{lid:{node:[current[lid,node][0]["observed_at_ns"],current[lid,node][1]] for node in endpoints[lid]} for lid in choice["link_path"]}})
                former[sid]=choice
            good_count+=1
    selections=[z["tool_choice"] for z in plans if "tool_choice" in z]
    model_choices=[ch for ch in selections if ch["modelCalled"]]
    if any(bool(ch["modelCalled"]) != (ch["method"] == "model") for ch in selections):
        raise ValueError("A model selection and its call receipt disagree")
    from model_component import validate_model_choice
    for ch in model_choices:
        validate_model_choice(ch, cond["rcPolicy"])
    if model_choices and cond["rcPolicy"].get("mode", "frozen")=="frozen":
        if len({ch.get("policyVersion", ch.get("parameterVersion")) for ch in model_choices})!=1:
            raise ValueError("Frozen policy changed version within a work order run")
    activated=[]
    for x in exe:
        cmd=x["command"]; action=cmd["action"]
        if x["status"]!="applied" or action["op"]!="activate":continue
        sid=action["service_id"];ep=action["epoch"]; origin=defs[sid];ns=[origin["srcNodeId"]];path=[]
        while ns[-1]!=origin["dstNodeId"]:
            hits=[y for y in routes if y["node_id"]==ns[-1] and y["service_id"]==sid and y["epoch"]==ep and y["time_ns"]<=x["time_ns"]]
            assert hits and hits[-1]["event"]=="install"; y=hits[-1];assert y["next_node_id"] not in ns
            path.append(y["link_id"]);ns.append(y["next_node_id"])
        ack=next((z for z in acks[cmd["command_id"]] if z["ack"].get("original_status",z["ack"]["status"])=="applied"),None)
        activated.append({"sid":sid,"epoch":ep,"path":path,"command":cmd["command_id"],"device_ns":x["time_ns"],
                          "ack_ns":ack["time_ns"] if ack else None,"issued_ns":cmd["issued_at_ns"],"payloadBps":action["reserved_bps"],"plan":cmd["plan_id"]})
    reads=[z for z in acts if z["event"]=="device_readback"]
    for x in reads:
        assert x["observed_at_ns"]<=x["arrived_at_ns"]<=x["time_ns"] and x["arrived_at_ns"]-x["observed_at_ns"]<=ttl
        cid=x["query_id"]
        assert any(v["command"]["action"]["op"]=="query" and v["status"]=="applied" and v["time_ns"]==x["observed_at_ns"] for v in by_id[cid])
        raw=next(v["ack"] for v in acks[cid] if v["time_ns"]==x["arrived_at_ns"] and v["ack"].get("b03_readback",{}).get("observed_at_ns")==x["observed_at_ns"])
        assert raw["b03_readback"]["result"]==x["result"]
        lookup=[y for y in routes if (y["service_id"],y["node_id"],y["epoch"])==(x["service_id"],x["node_id"],x["epoch"]) and y["time_ns"]<=x["observed_at_ns"]]
        expect=None if not lookup or lookup[-1]["event"]=="remove" else {k:lookup[-1][k] for k in ["link_id","next_node_id"]}
        assert x["result"]["forwarding"]==expect
    rpc=[x for x in acts if x["event"]=="b03_a2a"]
    for x in rpc:
        assert x["task_readback_matched"] and [(y["method"],y["http_status"]) for y in x["calls"]]==[("SendMessage",200),("GetTask",200)]
    work=[]
    for z in (x for x in acts if x["event"]=="work_started"):
        wid=z["work_id"];end=next((y for y in acts if y.get("work_id")==wid and y["event"] in ["work_verified","work_incomplete"]),None)
        steps=[{"phase":y["phase"],"sid":y["service_id"],"time_ns":y["time_ns"],"plan":y["plan_id"]} for y in acts if y["event"]=="work_step" and y["work_id"]==wid]
        if steps: assert min(y["time_ns"] for y in steps)>=z["solver_ready_at_ns"]
        signed=end is not None and end["event"]=="work_verified"
        if signed:
            assert end["om"]["configuration_matches"] and all(any(x["kind"]==k for x in rpc if x.get("work_id")==wid) for k in ["observe","control_request","verify"])
            for step in steps:
                row=next(x for x in acts if x["event"] in ["service_released","service_confirmed"] and x["work_id"]==wid and x["service_id"]==step["sid"] and x["phase"]==step["phase"])
                assert set(row["decision"]["node_path"])==set(row["query_ids"])
                assert all(any(x["work_id"]==wid and x["matched"] and x["query_id"]==q and x["arrived_at_ns"]<=row["time_ns"] for x in reads) for q in row["query_ids"].values())
        work.append({"id":wid,"release":z["release_ids"],"install":z["install_ids"],"authorized":z["authorized"],
                 "depends":z["dependencies"],"submit_ns":z["time_ns"],"solver_wall_ns":z["solver_wall_ns"],
                 "solver_ready_at_ns":z["solver_ready_at_ns"],"steps":steps,"verified":signed,
                 "verified_ns":None if not end else end["time_ns"],"snapshot":z["snapshot_id"]})
    if mode=="workorder" and summary["b03LoopStatus"]["planning_calls"]:
        assert len(rpc)>=2 and plans, "Missing role or planning ledger"
    if mode=="workorder" and packet["data"]["generated"]:
        assert activated and reads and routes, "Missing device evidence for an active source"
    held=summary["fault"]
    if held:
        cid=held["command_id"];ee=by_id[cid];assert len(sends[cid])>=2 and any(y["retry"] for y in sends[cid])
        assert sum(x["status"]=="applied" for x in ee)==1 and any(x["status"]=="duplicate" and x["before_version"]==x["after_version"] for x in ee)
        duplicate=next(y for y in acks[cid] if y["ack"]["status"]=="duplicate")
        held=dict(held,issuedNs=[x["time_ns"] for x in sends[cid]],device=[{k:y[k] for k in ["time_ns","status","before_version","after_version"]} for y in ee],
                  duplicateAckNs=duplicate["time_ns"],workRecovered=any(w["verified"] and held["plan_id"].startswith(w["id"]+"/") for w in work))
    latency=defaultdict(list)
    for pk in lines(run/"packets-control.jsonl"):
        kind=pk["message"]["kind"]
        if pk["event"]=="delivered" and kind in ["command","ack"]:
            latency[kind].append(pk["time_ns"]-pk["created_at_ns"])
    from lifecycle_evidence import check_lifecycle
    lifecycle = check_lifecycle(plans, acts, demand_index, cond, probe_index)
    from tech_knowledge import verify_work_order_queries
    knowledge_queries = verify_work_order_queries(cond, acts)
    return {"verification":verification,"lifecycle":lifecycle, **({"knowledgeQueries": knowledge_queries, "competitionMetrics": summary["competitionMetrics"]} if "competitionMetrics" in summary else {}),"scene":cond["scene"],"nodes":len(topo["nodes"]),"links":len(topo["links"]),"run":run.name,"mode":mode,"summarySha256":sha(run/"SUMMARY.json"),"business":summary["business"],
            "packets":packet,"authorizedConditions":cond["authority"],"code":cond.get("implementationSha256"),"seed":summary["seed"],"clock":clocks,"engine":summary.get("b03LoopStatus"),
            "afterQA":summary["releaseVerifiedAfterRun"],"nativeInputCondition":{key:cond[key] for key in ["originalConfigSha256","sourceHashes","simulation","officialDefinitions"]},
            "nativeReports":len(reports),"firstServiceReportNs":first_report,"firstPolicyVisibilityNs":visibility,"causalEvidenceChecks":dict(causal),"plansWithIncompleteLinkGraph":missing_samples,"acceptedPathChecks":good_count,
            "originalQosAndRoute":route_evidence,"physicalActivations":activated,"verifiedDeviceQueryCount":len(reads),
            "openingAdmission":[z["solver_audit"]["openingAdmission"] for z in plans if "openingAdmission" in z.get("solver_audit",{})],
            "actualModelCalls":len(model_choices),"toolChoices":dict(Counter(x["selected"] for x in selections if x["selected"])),
            "modelChoices":[{k:ch[k] for k in ("policyVersion","parameterVersion","selected","legalProbabilities","behaviorProbability","inputTokens","inputSha256","selectionWallNs") if k in ch} for ch in model_choices],
            "selectionWallNs":sum(ch["selectionWallNs"] for ch in selections),"solverWallNs":sum(z.get("solver_wall_ns",0) for z in plans),"a2aRpcWallNsInclusive":sum(z["wall_ns"] for z in rpc),"roleExchanges":len(rpc),"roleKinds":dict(Counter(x["kind"] for x in rpc)),"eventCounts":dict(tally),
            "work":work,"blockedAck":held,"networkPacketLatencyNs":{k:{"count":len(v),"sum":sum(v),"max":max(v)} for k,v in latency.items()},
            "sources":{p.name:sha(p) for p in run.iterdir() if p.is_file() and p.suffix in [".json",".jsonl"]}}


