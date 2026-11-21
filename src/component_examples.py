"""Read actual model and rule input/output pairs from a sealed work-order trace."""
import json

from integrity import check_archive
from tech_knowledge import query


def examples(directory, components):
    conditions = json.loads((directory / "CONDITIONS.json").read_bytes())
    check_archive(directory, conditions)
    if conditions["components"] != components.identity:
        raise ValueError("Use the component manifest that produced this trace")
    model = rule = None
    with (directory / "b03.jsonl").open(encoding="utf-8") as trace:
        for line in trace:
            row = json.loads(line)
            if row.get("event") != "b03_rc_plan":
                continue
            choice = row.get("tool_choice", {})
            if model is None and choice.get("modelCalled"):
                model = {"work":row["work_id"], "timeNs":row["time_ns"], "input":choice["observation"],
                         "output":{k:choice[k] for k in ("selected", "policyVersion", "behaviorProbability","legalProbabilities") if k in choice}}
            if rule is None and row.get("component_io"):
                rule = {"work":row["work_id"], "timeNs":row["time_ns"], **row["component_io"]}
            if rule is not None and (model is not None or conditions["rcPolicy"]["kind"] == "rule"):
                break
    if rule is None:
        raise ValueError("Choose an active run recorded with --record-component-io")
    return {"schema":"b03.component.examples.v1", "model":model, "rules":rule,
            "knowledge":{"input":{"id":"K045","version":components.knowledge["version"]},
                         "output":query(components, identifier="K045")}}
