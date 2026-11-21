"""Run and inspect one official B03 scenario with the pinned packet simulator."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

from scene import DEPENDENCY, PROFILE, ROOT, prepare, sha256 as checksum, simulator_root, verify_engine, write_json as json_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    run = actions.add_parser("run", help="Run the native packet clock and B03 controls")
    run.add_argument("--simulator", type=Path, default=os.environ.get("B03_SIMULATOR"), help="Read-only resnet_py release or its parent")
    run.add_argument("--data", type=Path, default=os.environ.get("B03_DATA"), help="Official dataset archive, extracted root or scenario folder")
    run.add_argument("--script", default=PROFILE["script"])
    run.add_argument("--mode", choices=["package", "single", "workorder"], default="workorder")
    run.add_argument("--initial-admission", choices=["residual","audit","original"], default="residual",
                     help="At the first admitted work order, check authorized omissions on residual links")
    run.add_argument("--policy", default="I",
                     help="Authorized RC tool, conditional D/K/J rule, or frozen checkpoint")
    run.add_argument("--checkpoint",type=Path,default=os.environ.get("B03_CHECKPOINT"))
    run.add_argument("--tokenizer",type=Path,default=os.environ.get("B03_TOKENIZER"))
    run.add_argument("--device",choices=["cpu","cuda"],default="cuda")
    run.add_argument("--services", default=",".join(PROFILE["exampleServices"]), help="Comma-separated active workload, or all")
    run.add_argument("--writable-services", help="Subset that can receive writes")
    run.add_argument("--protected-services", default="")
    run.add_argument("--start", type=int, default=PROFILE["start"])
    run.add_argument("--end", type=int, default=PROFILE["end"])
    run.add_argument("--drain", type=int, default=PROFILE["drain"])
    run.add_argument("--drop-install-ack", action="store_true", help="Test one lost native install receipt")
    run.add_argument("--output", type=Path, required=True)
    check = actions.add_parser("verify", help="Validate causal evidence, native device writes, QoS, and packet accounts")
    check.add_argument("directory", type=Path)
    check.add_argument("--report", type=Path, help="New file for complete per-service verification results")
    run.add_argument("--components", type=Path, help="Component manifest (default: config/components.json)")
    run.add_argument("--record-component-io", action="store_true", help="Save exact solver input and output for local reproduction")
    lookup = actions.add_parser("knowledge", help="Read a versioned technical knowledge entry")
    lookup.add_argument("--components", type=Path)
    selector = lookup.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", dest="identifier")
    selector.add_argument("--topic")
    sample = actions.add_parser("examples", help="Export actual model, solver and knowledge examples from a sealed run")
    sample.add_argument("directory", type=Path)
    sample.add_argument("--components", type=Path)
    sample.add_argument("--output", type=Path, help="Write the examples to a new local file")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so all invariant checks execute")
    packet = ROOT / "src/packet"
    sys.path.insert(0, str(packet))
    if args.command == "verify":
        from verify_packet import verify
        result = verify(args.directory.resolve())
        if args.report:
            json_file(args.report, result)
        print(json.dumps({k: result[k] for k in ("scene", "nodes", "links", "mode", "acceptedPathChecks", "verifiedDeviceQueryCount", "actualModelCalls", "toolChoices", "business", "packets")}, ensure_ascii=False))
        return
    from component_config import load_components
    components = load_components(args.components)
    if args.command == "knowledge":
        from tech_knowledge import query
        print(json.dumps(query(components, identifier=args.identifier, topic=args.topic), ensure_ascii=False, indent=2))
        return
    if args.command == "examples":
        from component_examples import examples
        result = examples(args.directory.resolve(), components)
        if args.output:
            json_file(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.policy not in (*components.symbols, "model", "combined"):
        run.error("The requested policy is absent from the configured action catalog")
    if not args.simulator:
        run.error("Supply --simulator or B03_SIMULATOR")
    dependency = simulator_root(args.simulator)
    out = args.output.resolve()
    raw_root = args.data.resolve() if args.data and args.data.is_dir() else None
    if out == dependency or dependency in out.parents or (raw_root and (out == raw_root or raw_root in out.parents)):
        run.error("Select an output directory outside the source inputs")
    if args.drain < 0 or not 0 <= args.start < args.end <= PROFILE["horizonSeconds"]:
        run.error("Require 0 <= start < end <= the scenario horizon, and drain >= 0")
    if f"scripts/script-{args.script}.json" not in PROFILE["pins"]:
        run.error("Choose a registered official script from 01 through 10")
    fingerprint = verify_engine(dependency)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(exist_ok=False)
    runtime = ROOT / "src/rules"
    os.environ["B03_PACKET_RUNTIME"] = str(runtime)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(dependency / "src"), str(runtime / "vendor/src"), str(runtime)]
    import resnet
    if not Path(resnet.__file__).resolve().is_relative_to((dependency / "src").resolve()):
        raise ValueError("Native simulator import origin differs from the pinned external source")
    from resnet.control.policy import decide
    from b03 import solver
    from packet_measure import MeasuredSimulation, request_row
    scenario, config, config_hash, input_dir = prepare(dependency, args.data, args.script, out)

    if args.mode != "workorder" and args.policy != "I":
        run.error("--policy is available on the joint OM/RC work-order path")
    from frozen_policy import DecisionPolicy
    chooser = DecisionPolicy(args.policy, args.checkpoint, args.tokenizer, args.device, args.initial_admission,
                             components=components) if args.mode == "workorder" else None
    if chooser is not None:
        from component_config import digest
        runtime_components = components.identity
        chooser.identity["workOrderComponentsSha256"] = digest({
            k:runtime_components[k] for k in ("catalog", "model", "rules", "providerModuleSha256")
        })
        chooser.record_component_io = args.record_component_io
    specs = {x.service_id: x for x in scenario.services + scenario.added_services}
    ids = sorted(specs) if args.services == "all" else sorted(set(args.services.split(",")))
    if not ids or not set(ids) <= specs.keys():
        run.error("Unknown or empty original service selection")
    writable = set(ids) if args.writable_services is None else set(filter(None, args.writable_services.split(",")))
    discover_writes = args.services == "all" and args.writable_services is None
    protected = set(filter(None, args.protected_services.split(",")))
    if not writable <= set(ids) or not protected <= set(ids):
        run.error("Write scope and protected IDs must be part of the original selected workload")
    if args.mode != "workorder" and (args.writable_services is not None or protected):
        run.error("Scoped B03 work-order permissions require --mode workorder")
    config.update(start_s=args.start, end_s=args.end, drain_s=args.drain, service_ids=ids)
    config["control"] = {**config["control"], "managed_service_ids": ids}
    pyenv = {pkg: importlib.metadata.version(pkg) for pkg in ["simpy"] + (["a2a-sdk"] if args.mode == "workorder" else [])}
    identity = {"scene": scenario.scenario_id, "script": args.script, "nodeCount": len(scenario.nodes), "linkCount": len(scenario.links), "initialServiceCount": len(scenario.services), "eventCount": len(scenario.events), "case":"packet_control", "sourceHashes": scenario.source_hashes,
                "components": components.identity, "rcPolicy": chooser.identity if chooser else None, "releaseManifestSha256": fingerprint, "originalConfigSha256": config_hash,
                "simulation": config, "services": ids, "officialDefinitions": {sid: request_row(specs[sid]) for sid in ids},
                "authority": {"discovery":"all_delivered_registered" if discover_writes else "explicit_roster", "allowedServiceIds": sorted(writable), "protectedServiceIds":sorted(protected), "effectiveWriteIds":sorted(writable - protected), "healthyMigration": False, "preemption": False,
                              "rateScale": config["rate_scale"], "scope": "B03 work orders; package/single use native stock review authority"},
                "coldStart": args.start != 0, "match": "same input bytes, reporting, start/end/drain, clocks, eligible IDs, full demand, physical queues and seed"}
    from integrity import source_identity
    implementation = source_identity()
    json_file(out / "CONDITIONS.json", {**identity, "implementationSha256": implementation, "mode": args.mode, "dropInstallAckAtDestination": args.drop_install_ack,
                                       "python": sys.version, "packages": pyenv})
    with ExitStack() as stack:
        traj = stack.enter_context((out / "b03.jsonl").open("x", encoding="utf-8"))
        def emit(row):
            traj.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n")
        rule = None
        if args.mode in ("single", "package"):
            if args.mode == "single":
                from packet_roles import single_policy
                rule_method = single_policy
            else:
                def rule_method(view, services, **kw):
                    return decide(view, services, qos_profile="official", **kw)
            def rule(snapshot, services, **kw):
                now = kw["now_ns"]
                if len(services) != 1 or snapshot["sampled_for_decision_at_ns"] != now:
                    raise ValueError("Single-policy contract mismatch")
                seen = {key: {"usable": x["usable"], "status": x["reason"],
                              "reports": {n: None if v is None else [v["probe"]["observed_at_ns"], v["received_at_ns"], v["probe"]["seq"]]
                                          for n, v in x["reporters"].items()}}
                        for key, x in snapshot["links"].items()}
                if any(v and not 0 <= v[0] <= v[1] <= now for x in seen.values() for v in x["reports"].values()):
                    raise ValueError("Future observation exposed to policy")
                start = time.perf_counter_ns()
                result = rule_method(snapshot, services, **kw)
                emit({"event": "single_query_and_route", "time_ns": now, "service_id": services[0].service_id,
                      "input": request_row(services[0]), "reports":seen,"retained_wire_bps":kw["retained_wire_bps"],
                      "decision":result["decisions"][0],"solver_wall_ns":time.perf_counter_ns()-start,
                      "solver_clock_charge_ns":0,"policy":args.mode})
                return result
        sim = MeasuredSimulation(scenario, config, out, decision_policy=rule, drop_install_ack=args.drop_install_ack)
        stack.callback(sim.close)
        sim.start_meter(out,solver)
        stack.callback(lambda: sim.meter.file.close())
        if args.mode == "workorder":
            from packet_roles import PacketRoles
            from packet_device import install
            roles = PacketRoles(None if discover_writes else writable, emit, chooser=chooser, protected_ids=protected, control_headroom_fraction=config["control"]["control_headroom_fraction"])
            stack.callback(roles.close)
            workloop = install(sim, roles, emit)
        started = time.perf_counter()
        sim.advance(sim.end_ns)
        boundary = sim.summary()
        sim.advance(sim.stop_ns)
        drained = sim.summary()
        if not (drained["checks_passed"] and boundary["checks_passed"]):
            raise RuntimeError("Native packet conservation failed")
        evaluation = sim.meter.close()
        requested = sum(drained["demand_bits_by_service"].values())
        json_file(out/"SUMMARY.json",{"mode":args.mode,"seed":config["seed"],"business":evaluation,"demandBits":requested,
                  "endBoundary":boundary["conservation"],"afterDrain":drained["conservation"],
                  "packets":drained["totals"],"services":{i:drained["services"][i] for i in ids},
                  "links":drained["links"],"sourceDefinitions":identity["officialDefinitions"],
                  "fault":sim.dropped_ack,"clock":{"start_ns":sim.start_ns,"end_ns":sim.end_ns,"drain_end_ns":sim.stop_ns},
                  "control":drained["control"],
                  "b03LoopStatus":workloop.status() if args.mode == "workorder" and hasattr(workloop,"status") else None,
                  "timeAccounting":{"modelClock":"SimPy integer ns","deviceNetwork":"actual packet enqueue through arrival",
                                    "solver":"measured RC tool selection and solver ns charged before dispatch" if args.mode=="workorder" else "measured wall ns; stock policy hook charges zero simulated compute",
                                    "a2a":"loopback role RPC wall time, separate from simulated device network"},"wallSeconds":time.perf_counter()-started,
                  "releaseVerifiedAfterRun":verify_engine(dependency)==fingerprint})
        print(json.dumps({"mode":args.mode,"window":[args.start,args.end], "U":evaluation["U"],"W":evaluation["W"],
                          "data":drained["conservation"]["data"],"fault":sim.dropped_ack is not None},ensure_ascii=False))

    from integrity import seal
    seal(out)
