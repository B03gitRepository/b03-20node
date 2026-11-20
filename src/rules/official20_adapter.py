"""Pure T091 official-current-state/role boundary. No Runtime, solver or model import."""
import copy,hashlib,json,math
SCHEMA="t119.official100-train-current-task.v1"
CAPABILITY="b03.official100-live-role-adapter.t119.v1"
TOOLMAP={'agent_om': {'A': 'query_affected_scope', 'B': 'query_authorized_snapshot'}, 'agent_rc': {'A': 'hold_current', 'B': 'priority_greedy', 'C': 'bounded_search', 'D': 'backup_restore', 'E': 'pair_local', 'F': 'pair_local', 'G': 'path_milp', 'H': 'path_milp', 'I': 'pair_local', 'J': 'path_milp', 'K': 'pool_greedy', 'L': 'pool_greedy', 'M': 'priority_greedy', 'N': 'pool_greedy'}}
SNAPSHOT_KEYS={"nodes","links","requests","assignments","stateVersion","configVersion","objectiveVersion","observedAtSec"}
def clone(x):return copy.deepcopy(x)
def canonical(x):return json.dumps(x,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def require(ok,why):
    if not ok:raise ValueError(why)
def active_ids(s):
    t=s["observedAtSec"]
    return sorted(k for k,r in s["requests"].items() if not r.get("_deleted",False) and r.get("startAtSec",0)<=t and (r.get("endAtSec") is None or t<r["endAtSec"]))
def physical_key(s):
    # Time alone may advance while a call runs. Lifecycles/active set cannot be erased.
    return digest({"state":{k:v for k,v in s.items() if k!="observedAtSec"},"activeServiceIds":active_ids(s)})
def bind_current(snapshot,config,authority,*,run_id,work_order_id,attempt,evidence_epoch,source,unknown_environment=False):
    require(set(snapshot)==SNAPSHOT_KEYS and len(snapshot["nodes"])==100,"OFFICIAL100_SNAPSHOT_SHAPE")
    require(type(attempt) is int and attempt>0 and type(evidence_epoch) is int and evidence_epoch>=0,"ATTEMPT_EPOCH")
    require(type(unknown_environment) is bool and not unknown_environment,"UNKNOWN_ENVIRONMENT")
    require(source.get("kind")=="official_development" and source.get("scenarioKey")=="sce20261012-conf01/sce03" and source.get("scriptId") in ('script-04.json','script-05.json'),"SOURCE_SCOPE")
    require(config.get("semantics_id")=="b03.flow-qos-rss-nomove.v1" and config.get("profile",{}).get("jitter_aggregation")=="root_sum_squares","RSS_SEMANTICS")
    require(type(run_id) is str and run_id and type(work_order_id) is str and work_order_id,"RUN_WORK_ID")
    require(set(authority)=={"policyId","policyVersion","readableServiceIds","executableServiceIds","frozenHealthyServiceIds","protectedServiceIds","allowedMethods"},"AUTHORITY_SCHEMA")
    ids=set(snapshot["requests"])
    for key in ("readableServiceIds","executableServiceIds","frozenHealthyServiceIds","protectedServiceIds"):
        a=authority[key];require(type(a) is list and all(type(i) is str for i in a) and len(set(a))==len(a) and set(a)<=ids,"AUTHORITY_SCOPE")
    require(set(authority["executableServiceIds"])<=set(authority["readableServiceIds"]),"WRITE_OUTSIDE_READ_SCOPE")
    require(set(authority["protectedServiceIds"])==set(config["protected_service_ids"]),"PROTECTED_SCOPE_CHANGED")
    require(not set(authority["executableServiceIds"])&(set(authority["protectedServiceIds"])|set(authority["frozenHealthyServiceIds"])),"FROZEN_WRITE_AUTHORIZATION")
    require(authority["allowedMethods"]==['backup_restore', 'bounded_search', 'hold_current', 'pair_local', 'path_milp', 'pool_greedy', 'priority_greedy'],"METHOD_MAPPING")
    require(set(snapshot["assignments"])==ids,"ASSIGNMENT_SERVICE_SET")
    identity={"runId":run_id,"workOrderId":work_order_id,"attemptNumber":attempt,"evidenceEpoch":evidence_epoch,"sourceHash":digest(source),"physicalStateHash":physical_key(snapshot),"configHash":digest(config),"policyHash":digest(authority),"versions":{k:snapshot[k] for k in ("stateVersion","configVersion","objectiveVersion")}}
    canonical(identity)
    return {"schema":SCHEMA,"capability":CAPABILITY,"identity":identity,"sourceSnapshotHash":digest(snapshot),"snapshot":clone(snapshot),"config":clone(config),"authority":clone(authority),"source":clone(source)}
def query_current(bound,tool,affected,assessment):
    require(bound["schema"]==SCHEMA and tool in TOOLMAP["agent_om"].values(),"QUERY_CAPABILITY")
    allowed=set(bound["authority"]["readableServiceIds"]);selected=sorted(allowed if tool=="query_authorized_snapshot" else set(affected))
    require(set(selected)<=allowed,"QUERY_OUTSIDE_AUTHORIZATION")
    active=set(active_ids(bound["snapshot"]));require(set(assessment["services"])==active,"ASSESSMENT_SCOPE")
    states={sid:("inactive" if sid not in active else "passed" if assessment["services"][sid]["satisfied"] is True else "failed") for sid in selected}
    return {"schema":"t091.actual-query.v1","identity":clone(bound["identity"]),"sourceSnapshotHash":bound["sourceSnapshotHash"],"tool":tool,"coveredServiceIds":selected,"completeActiveScope":active<=set(selected),"statuses":states,"assessmentHash":digest(assessment),"readOnly":True}
def role_request(bound,role,sequence,*,affected,diagnosis=None):
    require(role in TOOLMAP and type(sequence) is int and sequence>0,"ROLE_SEQUENCE")
    require(set(affected)<=set(bound["authority"]["readableServiceIds"]),"AFFECTED_SCOPE")
    if role=="agent_rc":
        require(diagnosis is not None and diagnosis["identity"]==bound["identity"] and diagnosis["sourceSnapshotHash"]==bound["sourceSnapshotHash"] and diagnosis["completeActiveScope"] is True,"RC_REQUIRES_CURRENT_COMPLETE_DIAGNOSIS")
    i=bound["identity"];mid=i["runId"]+"/"+i["workOrderId"]+"/"+str(i["attemptNumber"])+"/"+role+"/"+str(sequence)
    summary={"affectedServiceIds":sorted(affected),"activeRequestCount":len(active_ids(bound["snapshot"])),"nodeCount":len(bound["snapshot"]["nodes"]),"linkCount":len(bound["snapshot"]["links"]),"readableServiceIds":clone(bound["authority"]["readableServiceIds"]),"frozenHealthyCount":len(bound["authority"]["frozenHealthyServiceIds"]),"protectedCount":len(bound["authority"]["protectedServiceIds"]),"solverBudgets":{k:bound["config"][k] for k in ("path_limit","path_expansion_budget","search_budget","search_service_limit","max_path_hops")}}
    if diagnosis is not None:summary["diagnosis"]=clone(diagnosis)
    payload={"schema":"t091.role-current-evidence.v1","role":role,"identity":clone(i),"snapshotId":bound["sourceSnapshotHash"],"decisionId":mid+"/decision","mappingVersion":"t096.structured-rc-method.v1","toolMapHash":digest(TOOLMAP),"legalSymbols":list(TOOLMAP[role]),"visible":summary}
    return {"schema":"t091.role-envelope.v1","capability":CAPABILITY,"messageId":mid,"sender":"orchestrator" if role=="agent_om" else "agent_om","receiver":role,"identity":clone(i),"sourceSnapshotHash":bound["sourceSnapshotHash"],"modelObservation":payload,"observationHash":digest(payload),"payloadHash":digest(payload)}
def accept_choice(request,reply,current_bound):
    require(request["capability"]==CAPABILITY and current_bound["schema"]==SCHEMA,"CAPABILITY")
    require(reply.get("executionKind")=="actual_role_call","CACHE_OR_UNPROVEN_REPLY")
    require(reply.get("requestHash")==digest(request) and reply.get("messageId")==request["messageId"]+"/result" and reply.get("observationHash")==request["observationHash"],"REPLY_BINDING")
    require(reply.get("identity")==request["identity"] and current_bound["identity"]==request["identity"],"STALE_RESULT")
    require(reply.get("role")==request["receiver"] and reply.get("mappingVersion")=="t096.structured-rc-method.v1","ROLE_OR_MAPPING")
    symbol=reply.get("symbol");require(symbol in request["modelObservation"]["legalSymbols"],"ILLEGAL_SYMBOL")
    require(type(reply.get("isModelDecision")) is bool,"DECISION_KIND")
    return {"tool":TOOLMAP[reply["role"]][symbol],"isModelDecision":reply["isModelDecision"],"identity":clone(current_bound["identity"]),"notAnExecutionPermit":True}
def candidate_scope(bound,current_bound,target):
    require(bound["identity"]==current_bound["identity"],"STALE_CANDIDATE")
    s=current_bound["snapshot"];require(set(target)==set(s["requests"]),"TARGET_SERVICE_SET")
    changed=sorted(k for k in target if target[k]!=s["assignments"][k])
    require(set(changed)<=set(bound["authority"]["executableServiceIds"]),"CANDIDATE_OUTSIDE_AUTHORIZATION")
    # Existing Runtime.validate_plan + per-prefix submit_action remain mandatory.
    return {"changedServiceIds":changed,"scopePassed":True,"physicalGuardStillRequired":True,"configurationWriter":False}
