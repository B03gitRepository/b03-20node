"""Test a frozen adapter with standard Python and the synthetic work order."""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def decode(table):
    out = []
    for key, mask, *replacements in table["rows"]:
        values = list(table["defaults"])
        if mask < 0 or mask >> len(values) or mask.bit_count() != len(replacements):
            raise ValueError("Invalid column override mask")
        updates = iter(replacements)
        for i in range(len(values)):
            if mask & (1 << i):
                values[i] = next(updates)
        out.append(dict(zip(table["columns"], [key, *values], strict=True)))
    return out


def checked(actor, obs):
    original = deepcopy(obs)
    output = actor.choose(obs)
    if not isinstance(output, (list, tuple)) or len(output) != 2:
        raise ValueError("Return a tool ID and its metadata")
    action, meta = output
    if obs != original or action not in original["legalActions"] or not isinstance(meta, dict):
        raise ValueError("Observation changed or action is outside its delivered legal catalog")
    version = meta.get("policyVersion", meta.get("parameterVersion"))
    if not ((isinstance(version, str) and version.strip()) or (type(version) is int and version >= 0)):
        raise ValueError("Supply the actual policy version")
    if "parameterVersion" in meta and meta["parameterVersion"] != version:
        raise ValueError("Conflicting parameter and policy versions")
    json.dumps(output, allow_nan=False)
    return [action, meta]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=ROOT / "src/standalone_selector.py")
    parser.add_argument("--factory", default="build_model")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("candidate", args.adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = json.loads((HERE / "case.json").read_text(encoding="utf-8"))
    obs = case["observation"]
    config = {"id": "example.standalone-selector", "version": "1.0.0", "mode": "frozen",
              "entrypoint": "standalone_selector:build_model", "catalogVersion": "b03.tools.v1",
              "symbols": list(obs["legalActions"])}
    actor = getattr(module, args.factory)(checkpoint=None, tokenizer=None, device="cpu", config=config)
    tables = {k: decode(obs[k]) for k in ("nodes", "links", "services", "configs")}
    assert [len(tables[k]) for k in tables] == [3, 3, 2, 1]
    writable = {row["id"] for row in tables["services"] if row["writable"] and not row["protected"]}
    assert all(set(group) <= writable for group in obs["authorizedServiceSets"])
    sampled = checked(actor, deepcopy(obs))
    if args.adapter.resolve() == (ROOT / "src/standalone_selector.py").resolve():
        assert sampled == case["expected"]
    for only in obs["legalActions"]:
        subset = dict(obs, legalActions=[only], actionCatalog={only: obs["actionCatalog"][only]})
        single = checked(actor, deepcopy(subset))
        assert single[0] == only
    print(json.dumps({"cases": 4, "firstOutput": sampled, "tables": list(tables)}))


if __name__ == "__main__":
    main()
