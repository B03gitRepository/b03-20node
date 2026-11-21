"""Seal packet traces, audit their archived identities, or check an installed source environment."""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re

from component_config import digest, work_order_components_digest
from provenance import SCHEMA, active_kinds, changed, model_assets, revision, verify_provenance
from scene import DEPENDENCY, PROFILE, ROOT, sha256, simulator_root, verify_engine, write_json


def source_identity():
    """Preserve the full historical checkout inventory for earlier tools and recovery."""
    paths = [ROOT / "run.py", *(ROOT / "config").glob("*.json"),
             *(ROOT / "knowledge").glob("*.json"), *(ROOT / "src").rglob("*.py")]
    if (ROOT / "train.py").is_file():
        paths.append(ROOT / "train.py")
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(paths)}


def seal(run):
    files = {p.relative_to(run).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(run.rglob("*")) if p.is_file()}
    write_json(run / "RUN_FILES.json", {"schema": "b03.packet.run-files.v1", "files": files})


def check_members(run, conditions):
    record = json.loads((run / "RUN_FILES.json").read_bytes())
    if record.get("schema") != "b03.packet.run-files.v1":
        raise ValueError("Unknown packet evidence schema")
    files = record["files"]
    required = {"CONDITIONS.json", "SUMMARY.json", "b03.jsonl", "business-intervals.jsonl",
                "input/net-topo.json", "input/bg-service.json", f"input/scripts/script-{conditions['script']}.json"}
    optional = {"controller.jsonl", "executions.jsonl", "routes.jsonl", "packets-control.jsonl",
                "input/b03-demand-ends.json"}
    required.update(name for name in optional if (run / name).exists())
    if not required.issubset(files):
        raise ValueError("Evidence index is missing: " + ", ".join(sorted(required - files.keys())))
    root = run.resolve()
    for name, info in files.items():
        rel = Path(name)
        path = (root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Missing or invalid evidence member: " + name)
        if path.stat().st_size != info["bytes"] or sha256(path) != info["sha256"]:
            raise ValueError("Packet evidence digest mismatch: " + name)
    if conditions != json.loads((run / "CONDITIONS.json").read_bytes()):
        raise ValueError("Supplied conditions differ from the sealed CONDITIONS.json")
    return files


def check_official_inputs(run, conditions, files):
    if (conditions["nodeCount"], conditions["linkCount"]) != (PROFILE["nodes"], PROFILE["links"]):
        raise ValueError("Run belongs to a different network scale")
    script = conditions["script"]
    if conditions["scene"] != PROFILE["officialScenario"] + "/script-" + script:
        raise ValueError("Unknown official scenario for this network scale")
    from simulator_component import archived_engine_lock
    archived_engine_lock(run, conditions, files, default_lock=DEPENDENCY)
    for name in ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json"):
        if (name not in PROFILE["pins"] or conditions["sourceHashes"].get(name) != PROFILE["pins"][name]
                or files["input/" + name]["sha256"] != PROFILE["pins"][name]):
            raise ValueError("Original official input identity mismatch: " + name)
    overlay = conditions.get("activityOverlay")
    member = "input/b03-demand-ends.json"
    if member in files or overlay is not None:
        if (member not in files or not isinstance(overlay, dict) or overlay.get("schema") != "b03.demand-ends.v1"
                or overlay != json.loads((run / member).read_bytes())):
            raise ValueError("Activity overlay differs between CONDITIONS.json and " + member)


def valid_pins(value):
    return (isinstance(value, dict) and bool(value)
            and all(isinstance(k, str) and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin)
                    for k, pin in value.items()))


def native_receipt(conditions):
    policy = conditions.get("rcPolicy") or {}
    if policy.get("kind") != "frozen-model":
        return
    weights = policy.get("checkpointFilesSha256")
    if (not valid_pins(weights) or
            policy.get("modelSpecSha256") != conditions["implementationSha256"].get("config/model.json")):
        raise ValueError("Invalid archived native model specification or weight identity")
    public = ROOT / "config/model.json"
    if policy["modelSpecSha256"] == sha256(public):
        spec = json.loads(public.read_bytes())
        if spec["format"] == "hf-safetensors":
            pins = spec["weightFilesSha256"]
        else:
            record = spec["nativeVersions"].get(str(policy["version"]))
            if not record:
                raise ValueError("Unknown frozen policy version in the registered native model")
            pins = {"training.pt": record["checkpointSha256"], "metadata.json": record["metadataSha256"]}
        if pins != weights:
            raise ValueError("Frozen policy weight hashes differ from its registered public model specification")


def check_recorded(conditions):
    components = conditions.get("components")
    proof = conditions.get("provenance")
    if not valid_pins(conditions.get("implementationSha256")):
        raise ValueError("Missing or invalid implementation file inventory")
    if components is not None:
        expected = digest({k: v for k, v in components.items() if k != "manifestSha256"})
        if components.get("manifestSha256") != expected:
            raise ValueError("Archived component manifest fingerprint differs")
    policy = conditions.get("rcPolicy") or {}
    if "workOrderComponentsSha256" in policy:
        if components is None or policy["workOrderComponentsSha256"] != work_order_components_digest(components):
            raise ValueError("Work-order component fingerprint differs from the archived components")
    native_receipt(conditions)
    if proof is None:
        return
    if components is None or proof.get("schema") != SCHEMA:
        raise ValueError("Unsupported component provenance schema")
    if proof.get("fingerprint") != digest({k: v for k, v in proof.items() if k != "fingerprint"}):
        raise ValueError("Archived provenance fingerprint differs")
    rc = conditions.get("rcPolicy") or {}
    policy = (rc["method"] if rc.get("kind") == "rule" else "model") if conditions["mode"] == "workorder" else conditions["mode"]
    if proof["policy"] != policy or proof["nativeFrozen"] != (rc.get("kind") == "frozen-model"):
        raise ValueError("Archived active component path differs from the run policy")
    selected = active_kinds(policy, components)
    if set(proof["providers"]) != set(selected):
        raise ValueError("The provenance provider inventory differs from the selected policy")
    for kind in selected:
        entry = proof["providers"][kind].get("entrySha256")
        if entry != components["providerModuleSha256"].get(kind):
            raise ValueError(f"Recorded provider entry hash differs for {kind}")
        reg = dict(components[kind])
        if "assets" in reg.get("provenance", {}):
            reg["provenance"] = {**reg["provenance"], "assets": sorted(reg["provenance"]["assets"])}
        if reg != proof["activeComponents"][kind]:
            raise ValueError(f"Recorded effective settings differ for {kind}")
    if active_kinds(policy) and proof["activeComponents"]["catalog"] != components["catalog"]:
        raise ValueError("Recorded action catalog differs")
    for name, pin in proof["checkoutFiles"].items():
        if name in conditions["implementationSha256"] and conditions["implementationSha256"][name] != pin:
            raise ValueError("Recorded checkout hash differs for " + name)
    knowledge = proof["knowledge"]
    if (any(knowledge[k] != components["knowledge"][k] for k in ("id", "version", "sha256"))
            or hashlib.sha256(knowledge["text"].encode("utf-8")).hexdigest() != knowledge["sha256"]):
        raise ValueError("Archived knowledge edition or original bytes differ")
    if proof["nativeFrozen"] and proof["modelAssets"].get("checkpoint") != rc["checkpointFilesSha256"]:
        raise ValueError("Archived native checkpoint differs from the validated inference receipt")


def legacy_source(conditions, components, checkpoint, tokenizer):
    """A v1 receipt can establish only the source and provider files that it originally recorded."""
    differences = changed(conditions["implementationSha256"], source_identity(), "checkoutFiles")
    recorded = conditions.get("components")
    if recorded is not None:
        try:
            if components is None:
                from component_config import load_components
                components = load_components()
            differences.extend(changed(recorded, components.identity, "components"))
        except (ValueError, OSError, ImportError) as exc:
            differences.append(str(exc))
    for name, expected in conditions.get("packages", {}).items():
        try:
            differences.extend(changed(expected, metadata.version(name), "distributions." + name))
        except metadata.PackageNotFoundError:
            differences.append(f"distributions.{name}: missing")
    rc = conditions.get("rcPolicy") or {}
    if rc.get("kind") == "frozen-model":
        try:
            model_assets(ROOT, True, rc, checkpoint, tokenizer)
        except (ValueError, OSError, KeyError) as exc:
            differences.append(str(exc))
    return differences


def check_archive(run, conditions, *, strict_source=False, components=None, checkpoint=None, tokenizer=None, simulator=None):
    files = check_members(run, conditions)
    check_official_inputs(run, conditions, files)
    check_recorded(conditions)
    proof = conditions.get("provenance")
    simulator_check = "release-lock-only"
    if strict_source:
        engine = simulator or os.environ.get("B03_SIMULATOR")
        if conditions.get("simulatorRuntime") and not engine:
            raise ValueError("Supply --simulator or B03_SIMULATOR for strict engine verification")
        if engine:
            from simulator_component import archived_engine_lock
            lock = archived_engine_lock(run, conditions, files, default_lock=DEPENDENCY)
            actual = verify_engine(simulator_root(engine), lock) if conditions.get("simulatorRuntime") else verify_engine(simulator_root(engine))
            if actual != conditions["releaseManifestSha256"]:
                raise ValueError("Installed packet engine differs from the archived release")
            simulator_check = "installed-files"
        if proof:
            differences = verify_provenance(proof, ROOT, components, checkpoint=checkpoint, tokenizer=tokenizer,
                                            policy_identity=conditions.get("rcPolicy"))
        else:
            differences = legacy_source(conditions, components, checkpoint, tokenizer)
        if differences:
            message = "\n".join(differences[:30])
            if len(differences) > 30:
                message += f"\n… and {len(differences) - 30} further differences"
            raise ValueError("Strict source verification failed. Use the original checkout and environment.\n" + message)
    return {"mode": "strict-source" if strict_source else "archive", "archiveIndexSha256": sha256(run / "RUN_FILES.json"),
            "provenanceSchema": proof["schema"] if proof else "legacy", "sourceCompared": strict_source,
            "simulatorCheck": simulator_check, "recordedGitCommit": proof.get("gitCommit") if proof else None,
            "checkoutGitCommit": revision(ROOT) if strict_source else None}
