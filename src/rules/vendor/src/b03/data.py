"""Read official package with an explicit information boundary."""
from __future__ import annotations
import hashlib, json, math, re
from pathlib import Path
from .identity import SCENARIO,SCRIPTS

ARCHIVE_SHA256 = "0cb8e1db92a9e6df5be9765830aa4eabff5cc6ae6926c068852dfb829ab23255"
QOS = ("bandwidthBps", "delayMs", "jitterMs", "ber", "packetLossRate")
def digest_file(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1048576),b""):h.update(b)
    return h.hexdigest()
def canonical_hash(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
def normalize_qos(q, context):
    q=dict(q)
    if "bandwitdhBps" in q:
        if "bandwidthBps" in q and q["bandwidthBps"] != q["bandwitdhBps"]:raise ValueError(context+": conflicting bandwidth spellings")
        q["bandwidthBps"]=q.pop("bandwitdhBps")
    for k in QOS:
        v=q.get(k)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0:raise ValueError(context+": invalid "+k)
        if k in ("ber","packetLossRate") and v>1:raise ValueError(context+": probability > 1")
    return q
def validate_inputs(topology,requests,events):
    ids=[n["nodeId"] for n in topology["nodes"]]
    lids=[l["linkId"] for l in topology["links"]]
    sids=[s["serviceId"] for s in requests]
    for name, values in (("node",ids),("link",lids),("service",sids)):
        if any(not isinstance(i,str) or not i for i in values) or len(set(values))!=len(values):raise ValueError("Duplicate/invalid "+name+" id")
    for l in topology["links"]:
        if l["srcNodeId"] not in ids or l["dstNodeId"] not in ids:raise ValueError("Unknown link endpoint")
        l["params"]=normalize_qos(l["params"],l["linkId"])
    for s in requests:
        if s["srcNodeId"] not in ids or s["dstNodeId"] not in ids:raise ValueError("Unknown request endpoint")
        s["requirement"]=normalize_qos(s["requirement"],s["serviceId"])
        if isinstance(s.get("priority"),bool) or not isinstance(s.get("priority"),(int,float)) or not math.isfinite(s["priority"]):raise ValueError("Invalid priority")
        if s.get("startAtSec",0)<0:raise ValueError("Invalid lifetime")
        if s.get("endAtSec") is not None and s["endAtSec"]<s.get("startAtSec",0):raise ValueError("Invalid lifetime")
    if any(e.get('eventType')=='node_move' for e in events):raise ValueError('UNSUPPORTED_NODE_MOVE_PREFLIGHT')
    previous=-1
    for e in events:
        v=e["time"]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<previous:raise ValueError("Unsorted/invalid event time")
        if not isinstance(e.get("eventType"),str) or not isinstance(e.get("attrs"),dict):raise ValueError("Invalid event schema")
        previous=v
def selected_paths(scenario_key,script):
    if scenario_key!=SCENARIO or script not in SCRIPTS:raise ValueError('OUTSIDE_T089_CAPABILITY')
    return ('net-topo.json','bg-service.json','scripts/'+script)
def checked_blob(blob,expected):
    if len(blob)!=expected['bytes'] or hashlib.sha256(blob).hexdigest()!=expected['sha256']:raise ValueError('INPUT_MEMBER_IDENTITY_MISMATCH')
    return blob
def load_scenario(cache,scenario_key,script,verify_archive=True):
    names=selected_paths(scenario_key,script)
    if verify_archive is not True:raise ValueError('INPUT_VERIFICATION_REQUIRED')
    root=Path(__file__).resolve().parents[2];cache=Path(cache).resolve()
    if cache!=(root/'inputs').resolve():raise ValueError('FROZEN_CACHE_REQUIRED')
    locked=json.loads((root/'contracts/INPUT_MANIFEST.json').read_text(encoding='utf-8-sig'))
    if locked['scenarioKey']!=SCENARIO or locked['scripts']!=list(SCRIPTS) or locked['archiveSha256']!=ARCHIVE_SHA256:raise ValueError('INPUT_MANIFEST_SCOPE')
    expected={r['relativeInputPath']:r for r in locked['files']};raw={}
    for name in names:
        p=(cache/name).resolve()
        if not p.is_relative_to(cache):raise ValueError('CACHE_PATH_ESCAPE')
        raw[name]=checked_blob(p.read_bytes(),expected[name])
    topo,requests,events=[json.loads(raw[n]) for n in names];validate_inputs(topo,requests,events)
    source={'archiveSha256':ARCHIVE_SHA256,'scenarioKey':scenario_key,'script':script,'files':{n:{'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in raw.items()},'cacheManifestSha256':digest_file(root/'contracts/INPUT_MANIFEST.json'),'sourceKind':'official_development','inputAccess':'frozen_cache_sha_verified','boundary':'Only current topology and requests enter controller; full events stay in environment queue. No service-paths or postEvents input.'}
    return topo,requests,events,source
