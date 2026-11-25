"""Read actual model and rule input/output pairs from a sealed work-order trace."""
import json

from integrity import check_archive
from tech_knowledge import query_library
from scene import ROOT, sha256


def knowledge_example(conditions, components):
    identifier = "K045"
    proof = conditions.get("provenance")
    if proof:
        edition = proof["knowledge"]["version"]
        library = json.loads(proof["knowledge"]["text"])
    else:
        identity = conditions.get("components", {}).get("knowledge")
        if not identity:
            return {"input": {"id": identifier}, "output": None, "status": "knowledge-unrecorded"}
        edition = identity["version"]
        candidates = [components.knowledge_path] if components else sorted((ROOT / "knowledge").glob("*.json"))
        source = next((path for path in candidates if path.is_file() and sha256(path) == identity["sha256"]), None)
        if source is None:
            return {"input": {"id": identifier, "version": edition}, "output": None,
                    "status": "legacy-source-unavailable", "sourceSha256": identity["sha256"]}
        library = json.loads(source.read_bytes())
    return {"input": {"id": identifier, "version": edition},
            "output": query_library(library, edition, identifier=identifier)}


def examples(directory, components=None, *, strict_source=False, checkpoint=None, tokenizer=None, simulator=None):
    conditions = json.loads((directory / "CONDITIONS.json").read_bytes())
    check_archive(directory, conditions, strict_source=strict_source, components=components,
                  checkpoint=checkpoint, tokenizer=tokenizer, simulator=simulator)
    model = rule = knowledge = None
    ports = {}
    wanted_ports = set(conditions.get("components", {}).get("knowledge", {}).get("bindings", {}))
    if (conditions.get("rcPolicy") or {}).get("kind") == "rule":
        wanted_ports.discard("model")
    with (directory / "b03.jsonl").open(encoding="utf-8") as trace:
        for line in trace:
            row = json.loads(line)
            if row.get("event") != "b03_rc_plan":
                continue
            for port, record in row.get("knowledge_contexts", {}).items():
                ports.setdefault(port, record)
            patch = row.get("solver_audit", {}).get("rulePatch")
            if rule is None and patch:
                rule = {"work":row["work_id"], "timeNs":row["time_ns"], "schema":patch["schema"],
                        "input":patch["input"], "output":patch["output"]}
            if knowledge is None and row.get("knowledge_io"):
                knowledge = row["knowledge_io"]
            choice = row.get("tool_choice", {})
            if model is None and choice.get("modelCalled"):
                model = {"work":row["work_id"], "timeNs":row["time_ns"], "input":choice["observation"],
                         "output":{k:choice[k] for k in ("selected", "policyVersion", "parameterVersion", "behaviorProbability", "legalProbabilities") if k in choice}}
            if rule is None and row.get("component_io"):
                rule = {"work":row["work_id"], "timeNs":row["time_ns"], **row["component_io"]}
            if wanted_ports <= ports.keys() and knowledge is not None and rule is not None and (model is not None or (conditions.get("rcPolicy") or {}).get("kind") == "rule"):
                break
    if rule is None:
        raise ValueError("Choose an active run recorded with --record-component-io")
    active = conditions.get("provenance", {}).get("activeComponents", {})
    if knowledge is None:
        knowledge = ({"status": "opening-lookup-not-triggered"} if "knowledge" in active
                     else knowledge_example(conditions, components))
    result = {"schema":"b03.component.examples.v1", "model":model, "rules":rule, "knowledge":knowledge}
    if ports:
        result["knowledgePorts"] = ports
    return result
