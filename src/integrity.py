"""Seal and verify the native traces against the local source profile."""
import json
from pathlib import Path
from scene import DEPENDENCY, PROFILE, ROOT, sha256, write_json


def source_identity():
    paths = [ROOT/"run.py", *(ROOT/"config").glob("*.json"), *(ROOT/"src").rglob("*.py")]
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(paths)}


def seal(run):
    files={p.relative_to(run).as_posix():{"bytes":p.stat().st_size, "sha256":sha256(p)}
           for p in sorted(run.rglob("*")) if p.is_file()}
    write_json(run/"RUN_FILES.json",{"schema":"b03.packet.run-files.v1","files":files})


def check_archive(run, conditions):
    record=json.loads((run/"RUN_FILES.json").read_bytes())
    if record.get("schema")!="b03.packet.run-files.v1":
        raise ValueError("Unknown packet evidence schema")
    files=record["files"]
    required={"CONDITIONS.json","SUMMARY.json","b03.jsonl","business-intervals.jsonl","input/net-topo.json","input/bg-service.json"}
    if not required.issubset(files):
        raise ValueError("Evidence index is incomplete")
    for name,info in files.items():
        rel=Path(name)
        p=(run/rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not p.is_relative_to(run.resolve()) or not p.is_file():
            raise ValueError("Missing or invalid evidence member: "+name)
        if p.stat().st_size!=info["bytes"] or sha256(p)!=info["sha256"]:
            raise ValueError("Packet evidence digest mismatch: "+name)
    if (conditions["nodeCount"],conditions["linkCount"])!=(PROFILE["nodes"],PROFILE["links"]):
        raise ValueError("Run belongs to a different network scale")
    script=conditions["script"]
    if conditions["scene"]!=PROFILE["officialScenario"]+"/script-"+script or conditions["releaseManifestSha256"]!=DEPENDENCY["runtimeSha256"]:
        raise ValueError("Official scenario or simulator identity differs from this source profile")
    for n in ("net-topo.json","bg-service.json",f"scripts/script-{script}.json"):
        if n not in PROFILE["pins"] or conditions["sourceHashes"].get(n)!=PROFILE["pins"][n] or files["input/"+n]["sha256"]!=PROFILE["pins"][n]:
            raise ValueError("Original official input identity mismatch: "+n)
    expected=source_identity()
    policy=conditions.get("rcPolicy") or {}
    if policy.get("kind")=="frozen-model":
        pin=json.loads((ROOT/"config/model.json").read_bytes())
        if pin["format"]=="hf-safetensors":
            weights=pin["weightFilesSha256"]
        else:
            meta=pin["nativeVersions"][str(policy["version"])]
            weights={"training.pt":meta["checkpointSha256"],"metadata.json":meta["metadataSha256"]}
        if policy["modelSpecSha256"]!=expected["config/model.json"] or policy["checkpointFilesSha256"]!=weights:
            raise ValueError("Frozen policy identity differs from the recorded model or configuration")
    if conditions["implementationSha256"]!=expected:
        raise ValueError("This source checkout differs from the recorded implementation")
