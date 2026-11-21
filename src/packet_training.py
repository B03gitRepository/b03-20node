"""Run and verify a bounded online PPO update on authorized packet work orders."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from packet_learner import BASE, PacketLearner, _journal_lock, _load, _save, unweighted_advantage
from scene import PROFILE, ROOT


def read_json(path):
    return json.loads(path.read_bytes())


def write_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def json_lines(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def business_ledger(directory):
    rows = json_lines(directory / "business-intervals.jsonl")
    if not rows:
        raise ValueError("The packet meter recorded no scored intervals")
    previous_end = rows[0]["start_ns"]
    for row in rows:
        left, right = row["start_ns"], row["end_ns"]
        if (left != previous_end or right < left or
                row["U_service_ns"] != (right - left) * len(row["unmet"])):
            raise ValueError("The measured unweighted service clock is inconsistent")
        previous_end = right
    return rows


def measured_u(rows, start, end):
    return sum((min(row["end_ns"], end) - max(row["start_ns"], start)) * len(row["unmet"])
               for row in rows if row["end_ns"] > start and row["start_ns"] < end)


def samples_from_work_orders(actual, control, report):
    """Credit actual in-window draws against original same-condition fixed-I intervals."""
    a, b = (read_json(d / "CONDITIONS.json") for d in (actual, control))
    matched = ("nodeCount", "script", "sourceHashes", "releaseManifestSha256", "originalConfigSha256",
               "simulation", "services", "officialDefinitions", "authority", "mode", "dropInstallAckAtDestination")
    if (a["script"] not in ("04", "05") or a["nodeCount"] != 100 or a["mode"] != "workorder"
            or a["rcPolicy"].get("mode") != "online-ppo" or b["rcPolicy"].get("method") != "I"
            or any(a[k] != b[k] for k in matched)):
        raise ValueError("PPO needs a matched 100-node training work order and its fixed-I control")
    rows, control_rows = (business_ledger(d) for d in (actual, control))
    start, end = rows[0]["start_ns"], rows[-1]["end_ns"]
    if ((start, end) != (control_rows[0]["start_ns"], control_rows[-1]["end_ns"])
            or not math.isclose(measured_u(rows, start, end) / 1e9, report["business"]["U"],
                                rel_tol=0, abs_tol=1e-8)):
        raise ValueError("Credit windows and the verified unweighted U account must agree")
    decisions = [r["tool_choice"] for r in json_lines(actual / "b03.jsonl")
                 if r.get("event") == "b03_rc_plan" and (r.get("tool_choice") or {}).get("modelCalled")]
    if len(decisions) != report["actualModelCalls"]:
        raise ValueError("Trace and archive verifier count different model calls")
    samples = []
    for choice in decisions:
        obs = choice["observation"]
        at = obs["timeNs"]
        if not start <= at < end or len(obs["legalActions"]) < 2:
            continue
        own_ns, fixed_ns = measured_u(rows, at, end), measured_u(control_rows, at, end)
        credit = {"nodes": a["nodeCount"], "script": a["script"], "fromNs": at, "toNs": end,
                  "actualUnmetServiceNs": own_ns, "fixedIUnmetServiceNs": fixed_ns,
                  "rawReturnSeconds": -own_ns / 1e9, "fixedIReturnSeconds": -fixed_ns / 1e9}
        unweighted_advantage(credit)
        samples.append((choice, credit))
    if not samples:
        raise ValueError("No measured in-window draws among two or more authorized actions")
    return samples


def run_episode(directory, options, manifest, policy, on_policy_ready=None):
    """Use the standard simulator, OM/RC, device writes, readbacks and archive verifier."""
    from cli import main
    args = ["run", "--policy", policy, "--components", str(manifest), "--simulator", options["simulator"],
            "--data", str(data_locations(options["data"])[0]), "--script", options["script"], "--services", options["services"],
            "--start", str(options["start"]), "--end", str(options["end"]), "--drain", str(options["drain"]),
            "--output", str(directory)]
    if policy == "model":
        args.extend(["--checkpoint", options["checkpoint"], "--tokenizer", options["tokenizer"],
                     "--device", options["device"]])
    main(args, on_policy_ready=on_policy_ready)
    from verify_packet import verify
    report = verify(directory)
    write_json(directory.parent / (directory.name + "-verified.json"), report)
    return report


def next_draw(actor, observation):
    symbol, detail = actor.choose(observation)
    return {"symbol": symbol, **{k: detail[k] for k in ("sampleId", "sampleDraw", "parameterSha256",
            "parameterVersion", "inputSha256", "tokenIdsSha256", "samplingUniform", "legalProbabilities",
            "behaviorProbabilities", "behaviorProbability", "oldLogProbability")}}


def public_business(report):
    return {"U": report["business"]["U"], "W": report["business"]["W"],
            "actualModelCalls": report["actualModelCalls"], "readbacks": report["verifiedDeviceQueryCount"]}


def restore_actor(args, replay):
    actor = PacketLearner(args.checkpoint, args.tokenizer, args.device)
    info = actor.restore_checkpoint(replay)
    return actor, info


def finish_round(workspace, number, prepared, actor, journal, update, origin):
    snapshot = workspace / f"round-{number:03d}-replay.json"
    saved = actor.save_checkpoint(snapshot)
    head, _ = _load(snapshot)
    last = head["updates"][-1]["samples"][-1]
    receipt = {**prepared, "update": update, "saved": saved, "snapshot": snapshot.name,
               "nextDrawWitness": next_draw(actor, last["observation"]),
               "completion": origin, "finishedProcess": os.getpid()}
    write_json(workspace / f"round-{number:03d}.json", receipt)
    return receipt


def one_round(args, root):
    if PROFILE["nodes"] != 100:
        raise ValueError("The reference learner is registered for the 100-node checkpoint and training scripts")
    if not 1 <= args.round <= 16:
        raise ValueError("Use a round number in the bounded replay range 1 through 16")
    n = args.round
    options = {"script": args.script, "services": args.services, "start": args.start, "end": args.end,
               "drain": args.drain, "device": args.device,
               **{k: str(Path(getattr(args, k)).resolve(strict=True))
                  for k in ("components", "checkpoint", "tokenizer", "data", "simulator")}}
    journal = root / "ppo-replay.json"
    done = root / f"round-{n:03d}.json"
    prepared_path = root / f"round-{n:03d}-prepared.json"
    chain = _load(journal)[0]["updates"] if journal.is_file() else []
    if done.exists():
        receipt = read_json(done)
        if (len(chain) < n or receipt["options"] != options or
                receipt["saved"]["parameterSha256"] != chain[n - 1]["step"]["afterSha256"] or
                _load(root / receipt["snapshot"])[1] != receipt["saved"]["journalSha256"]):
            raise ValueError("Completed round conflicts with its supplied request or replay history")
        return receipt
    if len(chain) == n:
        # Recover an interruption after the atomic journal commit without resampling.
        prepared = read_json(prepared_path)
        if prepared["options"] != options:
            raise ValueError("The committed round used different options")
        actor, _ = restore_actor(args, journal)
        step = chain[-1]["step"]
        return finish_round(root, n, prepared, actor, journal, dict(step, sampleCount=len(chain[-1]["samples"]),
                            parameterVersion=actor.version, newOnlineUpdates=1), "recovered committed journal")
    if len(chain) != n - 1:
        raise ValueError("Run each round after its previous replay boundary")
    previous = root / f"round-{n-1:03d}-replay.json"
    if n > 1 and (not previous.is_file() or _load(previous)[1] != _load(journal)[1]):
        raise ValueError("Finish the previous round's immutable replay snapshot first")
    attempts = root / "attempts"
    attempts.mkdir(exist_ok=True)
    attempt = attempts / f"round-{n:03d}-{uuid.uuid4().hex[:10]}"
    attempt.mkdir()
    selection = {"extends": os.path.relpath(options["components"], attempt)}
    if n > 1:
        selection["model"] = {"provenance": {"assets": {"replay": os.path.relpath(previous, attempt)}}}
    manifest = attempt / "components.json"
    write_json(manifest, selection)
    from component_config import load_components
    registered = load_components(manifest)
    if registered.model["entrypoint"] != "packet_learner:build_model" or registered.model["mode"] != "online-ppo":
        raise ValueError("Select the registered reference PPO adapter for this trainer")
    baseline, work = attempt / "fixed-I", attempt / "sampled"
    reference = run_episode(baseline, options, manifest, "I")
    captured = []
    sampled = run_episode(work, options, manifest, "model", on_policy_ready=lambda chooser: captured.append(chooser.actor))
    if len(captured) != 1 or not isinstance(captured[0], PacketLearner) or captured[0].version != BASE + n - 1:
        raise ValueError("The registered work order did not run the expected policy version")
    actor = captured[0]
    samples = samples_from_work_orders(work, baseline, sampled)
    credits = [{"sampleId": d["sampleId"], "action": d["selected"], "policyVersion": d["policyVersion"],
                "behaviorProbability": d["behaviorProbability"], **c, "advantageSeconds": unweighted_advantage(c)}
               for d, c in samples]
    prepared = {"round": n, "options": options, "sampledProcess": os.getpid(),
                "componentInputPaths": sorted(str(path) for path in registered.input_paths),
                "parentVersion": actor.version, "parentSha256": actor.parameter_sha256,
                "workOrder": str(work.relative_to(root)), "fixedI": str(baseline.relative_to(root)),
                "evaluation": {"sampled": public_business(sampled), "fixedI": public_business(reference)},
                "credits": credits}
    write_json(prepared_path, prepared)
    began = time.monotonic()
    update = actor.update(samples, journal)
    update["optimizerWallSeconds"] = time.monotonic() - began
    return finish_round(root, n, prepared, actor, journal, update, "live committed update")


def restore_round(args, root):
    receipt = read_json(root / f"round-{args.round:03d}.json")
    replay = root / receipt["snapshot"]
    actor, evidence = restore_actor(args, replay)
    saved = receipt["saved"]
    body, checksum = _load(replay)
    if (checksum != saved["journalSha256"] or actor.parameter_sha256 != saved["parameterSha256"]
            or actor.version != saved["parameterVersion"] or not evidence["fullFingerprintVerified"]
            or evidence["nextDrawCursor"] != saved["nextDrawCursor"]):
        raise ValueError("The restored boundary differs from the live update")
    last = body["updates"][-1]["samples"][-1]
    witness = next_draw(actor, last["observation"])
    if witness != receipt["nextDrawWitness"]:
        raise ValueError("Restored next action, version or behavior probabilities differ from the live witness")
    result = {**evidence, "nextDrawMatched": True, "nextDrawWitness": witness,
              "savedProcess": receipt["finishedProcess"], "restoreProcess": os.getpid()}
    if args.report:
        target = args.report.resolve()
        if target.exists():
            raise ValueError("Select a new restore report path")
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, result)
    return result


def data_locations(value):
    """Identify the data asset inside either supported outer dataset layout."""
    src = Path(value).expanduser().resolve(strict=True)
    if src.is_dir() and not (src / "net-topo.json").is_file():
        for collection in (src / "data", src):
            scenario = collection / "scenarios" / PROFILE["officialScenario"]
            if (scenario / "net-topo.json").is_file():
                protected = collection if collection != src else collection / "scenarios"
                return scenario.resolve(strict=True), protected.resolve(strict=True)
    return src, src


def check_output_paths(outputs, assets):
    for output in outputs:
        path = Path(output).expanduser().resolve()
        if any(path == asset or asset in path.parents for asset in assets):
            raise ValueError(f"Training output overlaps a read-only input asset: {path}")


def training_workspace(args):
    """Resolve inputs and output destinations before creating directories or lock files."""
    from scene import simulator_root
    root = args.workspace.expanduser().resolve()
    assets = {Path(p).expanduser().resolve(strict=True) for p in (args.checkpoint, args.tokenizer)}
    base = f"round-{args.round:03d}"
    outputs = [root, root / (base + "-replay.json"), root / (base + "-replay.json.lock")]
    if args.command == "step":
        from component_config import PROVIDERS, load_components
        simulator_asset = simulator_root(args.simulator).resolve(strict=True)
        _, data_asset = data_locations(args.data)
        manifest = Path(args.components).expanduser().resolve(strict=True)
        components = load_components(manifest)
        assets.update((simulator_asset, data_asset))
        assets.update(components.input_paths)
        for kind in PROVIDERS:
            registration = getattr(components, kind, {})
            for path in registration.get("provenance", {}).get("assets", {}).values():
                assets.add(Path(path).expanduser().resolve(strict=True))
        outputs.extend(root / tail for tail in ("session.lock", "attempts", "ppo-replay.json", "ppo-replay.json.lock",
                                                base + ".json", base + "-prepared.json"))
        if args.round > 1:
            previous = root / f"round-{args.round - 1:03d}-replay.json"
            outputs.extend((previous, previous.with_name(previous.name + ".lock")))
    else:
        check_output_paths(outputs, assets)
        receipt = read_json(root / (base + ".json"))
        if (receipt["snapshot"] != base + "-replay.json" or
                not (root / receipt["snapshot"]).resolve().is_relative_to(root)):
            raise ValueError("The restore receipt must select this workspace's committed replay")
        # New receipts also remember the input paths after a component manifest moves or changes.
        assets.update(Path(path).resolve() for path in receipt.get("componentInputPaths", ()))
        # Resolve available manifest layers for older receipts, even when another input has moved.
        for key in ("data", "simulator", "checkpoint", "tokenizer", "components"):
            value = receipt.get("options", {}).get(key)
            if not value:
                continue
            path = Path(value).expanduser().resolve()
            if not path.exists():
                continue
            if key == "data":
                path = data_locations(path)[1]
            elif key == "simulator":
                path = simulator_root(path).resolve(strict=True)
            elif key == "components":
                from component_config import read_manifest
                read_manifest(path, inputs=assets, allow_missing=True)
            assets.add(path)
        if args.report:
            journal_files = {root / "ppo-replay.json", root / "attempts", *root.glob("round-*.json")}
            check_output_paths([args.report], journal_files)
            outputs.append(args.report)
    check_output_paths(outputs, assets)
    return root


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("step", help="Sample a new work order and commit one real PPO update")
    restore = sub.add_parser("restore", help="Verify every parameter and the next sampled action")
    for command in (run, restore):
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--round", type=int, required=True)
        command.add_argument("--checkpoint", type=Path, required=True, help="Pinned v374 base directory")
        command.add_argument("--tokenizer", type=Path, required=True)
        command.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    run.add_argument("--components", type=Path, default=ROOT / "config/packet-ppo.json")
    run.add_argument("--script", choices=["04", "05"], required=True)
    run.add_argument("--simulator", type=Path, required=True)
    run.add_argument("--data", type=Path, required=True)
    run.add_argument("--services", required=True, help="Original service IDs separated by commas, or all")
    run.add_argument("--start", type=int, required=True)
    run.add_argument("--end", type=int, required=True)
    run.add_argument("--drain", type=int, required=True)
    restore.add_argument("--report", type=Path)
    args = p.parse_args()
    if not __debug__:
        p.error("Run without -O so archive verification checks are active")
    root = training_workspace(args)
    if args.command == "step":
        root.mkdir(parents=True, exist_ok=True)
        with _journal_lock(root / "session"):
            result = one_round(args, root)
        print(json.dumps({k: result[k] for k in ("round", "parentVersion", "saved", "credits", "evaluation")}))
    else:
        result = restore_round(args, root)
        print(json.dumps(result))
