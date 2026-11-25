"""One full work order per sample. Trainer and evaluator have separate return types."""
from __future__ import annotations

import argparse
from bisect import bisect_right
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from scene import PROFILE, ROOT

# Research splits are by full development script, including every derived window.
TRAIN_SCRIPTS = {20: {"01", "02", "04", "05"}, 100: {"04", "05"}}


def _run(*arguments):
    subprocess.run([sys.executable, "-B", str(ROOT / "run.py"), *map(str, arguments)],
                   check=True, stdout=subprocess.DEVNULL)


def _verified(directory):
    with tempfile.TemporaryDirectory(prefix="b03-verify-") as tmp:
        report = Path(tmp) / "verified.json"
        _run("verify", Path(directory).resolve(), "--report", report)
        return json.loads(report.read_bytes())


def _jsonl(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def trajectory(intervals, choices, verified_u):
    """Partition the scored interval exactly once, including the prefix before any draw."""
    ends, totals, counts = [], [0], []
    start = None
    for row in intervals:
        lo, hi = row["start_ns"], row["end_ns"]
        if start is None:
            start = lo
        if lo != (ends[-1] if ends else start) or hi < lo:
            raise ValueError("Non-contiguous service clock")
        count = len(row["unmet"])
        accrued = (hi - lo) * count
        if row["U_service_ns"] != accrued:
            raise ValueError("Incorrect unweighted service clock")
        ends.append(hi)
        counts.append(count)
        totals.append(totals[-1] + accrued)
    if not ends or abs(totals[-1] / 1e9 - verified_u) > 1e-8:
        raise ValueError("Trajectory and verified U differ")

    def unmet_until(t):
        k = bisect_right(ends, t)
        if k == len(ends):
            return totals[k]
        return totals[k] + (t - (ends[k - 1] if k else start)) * counts[k]

    steps = []
    previous = None
    for choice in choices:
        obs = choice["observation"]
        t = obs["timeNs"]
        if previous is not None and t < previous:
            raise ValueError("Decision order differs from the simulated clock")
        previous = t
        if not start <= t < ends[-1]:
            continue
        if choice["selected"] not in obs["legalActions"]:
            raise ValueError("Unregistered action in verified trajectory")
        sample = {"observation": obs, "action": choice["selected"],
                  "policyVersion": choice.get("policyVersion", choice.get("parameterVersion"))}
        # Report only what the actor actually logged. Greedy actions do not imply a draw.
        for key in ("behaviorProbability", "behaviorProbabilities", "oldLogProbability", "sampleId"):
            if key in choice:
                sample[key] = choice[key]
        steps.append(sample)
    for i, step in enumerate(steps):
        end = steps[i + 1]["observation"]["timeNs"] if i + 1 < len(steps) else ends[-1]
        delta = unmet_until(end) - unmet_until(step["observation"]["timeNs"])
        step.update(untilNs=end, rewardUnweightedServiceNs=-delta, terminal=(i + 1 == len(steps)))
    first = steps[0]["observation"]["timeNs"] if steps else ends[-1]
    return {"startNs": start, "endNs": ends[-1], "prefixUnweightedServiceNs": unmet_until(first),
            "unmetServiceNs": totals[-1], "steps": steps}


def sample(script, output, *, components=None, data=None, simulator=None, checkpoint=None,
           tokenizer=None, device="cpu", start=None, end=None, drain=None, services=None):
    """Run a registered model on an authorized training script in a fresh worker."""
    if script not in TRAIN_SCRIPTS[PROFILE["nodes"]]:
        permitted = ", ".join(sorted(TRAIN_SCRIPTS[PROFILE["nodes"]]))
        raise ValueError(f"Permitted training scripts for this profile: {permitted}")
    directory = Path(output).resolve()
    args = ["run", "--policy", "model", "--script", script, "--output", directory, "--device", device]
    for key, value in (("components", components), ("data", data), ("simulator", simulator),
                       ("checkpoint", checkpoint), ("tokenizer", tokenizer), ("start", start),
                       ("end", end), ("drain", drain), ("services", services)):
        if value is not None:
            args.extend(["--" + key, value])
    _run(*args)
    report = _verified(directory)
    conditions = json.loads((directory / "CONDITIONS.json").read_bytes())
    decisions = [r["tool_choice"] for r in _jsonl(directory / "b03.jsonl")
                 if r.get("event") == "b03_rc_plan" and (r.get("tool_choice") or {}).get("modelCalled")]
    if len(decisions) != report["actualModelCalls"]:
        raise ValueError("Verifier and trajectory count different model calls")
    return {"schema": "b03.episode.sample.v1", "nodes": PROFILE["nodes"], "script": script,
            "actorMode": conditions["rcPolicy"]["mode"], "verifiedCalls": len(decisions),
            "archiveSha256": report["verification"]["archiveIndexSha256"],
            **trajectory(_jsonl(directory / "business-intervals.jsonl"), decisions, report["business"]["U"])}


def evaluate(directory):
    """Read-only evaluation for any registered development or training work order."""
    r = _verified(directory)
    return {"schema": "b03.episode.evaluation.v1", "nodes": r["nodes"], "scene": r["scene"],
            "archiveSha256": r["verification"]["archiveIndexSha256"], "business": r["business"],
            "competitionMetrics": r.get("competitionMetrics"), "toolChoices": r["toolChoices"],
            "modelCalls": r["actualModelCalls"], "readbacks": r["verifiedDeviceQueryCount"],
            "solverWallNs": r["solverWallNs"], "selectionWallNs": r["selectionWallNs"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(required=True, dest="command")
    train = subs.add_parser("sample", help="Run an authorized training work order and return unweighted transitions")
    train.add_argument("--output", required=True)
    train.add_argument("--script", required=True, choices=sorted(TRAIN_SCRIPTS[PROFILE["nodes"]]))
    for option in ("components", "data", "simulator", "checkpoint", "tokenizer", "services"):
        train.add_argument("--" + option)
    train.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    for option in ("start", "end", "drain"):
        train.add_argument("--" + option, type=int)
    score = subs.add_parser("evaluate", help="Verify a sealed run and return separate evaluation metrics")
    score.add_argument("directory")
    opts = vars(parser.parse_args())
    command = opts.pop("command")
    result = sample(**opts) if command == "sample" else evaluate(**opts)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
