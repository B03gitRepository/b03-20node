"""Read the supplied packet engine and build a run-local view of official B03 inputs."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "config/profile.json").read_bytes())
DEPENDENCY = json.loads((ROOT / "config/simulator.lock.json").read_bytes())


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signed_inputs(raw, script):
    names = ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json")
    if set(raw) != set(names) or any(name not in PROFILE["pins"] for name in names):
        raise ValueError("This official script is outside the registered profile")
    for name in names:
        if hashlib.sha256(raw[name]).hexdigest() != PROFILE["pins"][name]:
            raise ValueError(f"Official input identity mismatch: {name}")


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def simulator_root(value):
    path = Path(value).expanduser().resolve(strict=True)
    return path / "resnet_py" if (path / "resnet_py/src/resnet").is_dir() else path


def verify_engine(path):
    for name, expected in DEPENDENCY["files"].items():
        if not (path / name).is_file() or sha256(path / name) != expected:
            raise ValueError(f"Unexpected simulator source: {name}. Supply the pinned resnet_py release.")
    return DEPENDENCY["runtimeSha256"]


def official_bytes(value, script):
    if not re.fullmatch(r"[0-9]{2}", script):
        raise ValueError("Use a two-digit official script number")
    src = Path(value).expanduser().resolve(strict=True)
    names = ("net-topo.json", "bg-service.json", f"scripts/script-{script}.json")
    prefix = "data/scenarios/" + PROFILE["officialScenario"]
    if src.is_dir():
        choices = [src, src / prefix, src / "scenarios" / PROFILE["officialScenario"]]
        folder = next((p for p in choices if (p / "net-topo.json").is_file()), None)
        if folder is None:
            raise ValueError("Supply the raw official scenario directory or the extracted dataset root")
        return {name: (folder / name).read_bytes() for name in names}
    expected = {prefix + "/" + name: name for name in names}
    found = {}
    with tarfile.open(src, "r:*") as bundle:
        for member in bundle:
            key = PurePosixPath(member.name).as_posix()
            if key not in expected:
                continue
            name = expected[key]
            if name in found or not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError(f"Unexpected official archive member: {name}")
            with bundle.extractfile(member) as handle:
                found[name] = handle.read()
    if set(found) != set(names):
        raise ValueError(f"Missing official archive members: {sorted(set(names) - set(found))}")
    return found


def prepare(engine, data, script, output):
    """Keep the official files byte-identical. Add only native scenario and traffic metadata."""
    from resnet.application.config import load_config
    if data is None:
        if PROFILE["nodes"] != 20 or script != "09":
            raise ValueError("Supply --data or B03_DATA for this official scenario/script")
        scenario_dir = engine / "data/OFFICIAL20-script09"
        for name, expected in DEPENDENCY["bundled20"].items():
            if sha256(engine / name) != expected:
                raise ValueError(f"Unexpected bundled official input: {name}")
        scenario, config, _, identity = load_config(engine / "configs/official20.json", "official_control")
        # The auditor always uses the original three files that actually supplied this run.
        raw = official_bytes(scenario_dir, script)
        signed_inputs(raw, script)
        local = output / "input"
        local.mkdir()
        (local / "scripts").mkdir()
        for name, blob in raw.items():
            (local / name).write_bytes(blob)
        for n in ("scenario-config.json", "runtime-traffic.json", "provenance.json"):
            shutil.copyfile(scenario_dir / n, local / n)
    else:
        raw = official_bytes(data, script)
        signed_inputs(raw, script)
        local = output / "input"
        local.mkdir()
        (local / "scripts").mkdir()
        for name, blob in raw.items():
            (local / name).write_bytes(blob)
        topology = json.loads(raw["net-topo.json"])
        if len(topology["nodes"]) != PROFILE["nodes"]:
            raise ValueError(f"Expected {PROFILE['nodes']} physical nodes, found {len(topology['nodes'])}")
        initial = json.loads(raw["bg-service.json"])
        events = json.loads(raw[f"scripts/script-{script}.json"])
        duration = PROFILE["horizonSeconds"]
        if any(row.get("startAtSec") != 0 for row in initial):
            raise ValueError("This official activity profile requires all initial demands to start at zero")
        if any(row["time"] > duration for row in events):
            raise ValueError("The scenario horizon ends before an official event")
        additions = [{"service_id": row["attrs"]["serviceId"], "start_s": row["time"], "end_s": duration}
                     for row in events if row["eventType"] == "service_adjust" and row["attrs"].get("op") == "add"]
        traffic = {"schema": "project-traffic-activity-v1", "project_extension": True,
                   "definition": "Original initial requests are active from zero. Added requests start at their official event; the experiment horizon is explicit.",
                   "realtime_types": ["voice", "video"], "profiles": {"official_all": {"label": "full demand", "active_service_ids": [s["serviceId"] for s in initial]}},
                   "profile_windows": [{"start_s": 0, "end_s": duration, "profile": "official_all"}], "temporary_windows": additions}
        write_json(local / "runtime-traffic.json", traffic)
        write_json(local / "scenario-config.json", {"scenario_id": PROFILE["officialScenario"] + "/script-" + script,
                   "source_scenario": PROFILE["officialScenario"], "official_dataset": True, "duration_s": duration,
                   "input_mode": "official_script", "movement_semantics": "endpoint_at_event",
                   "files": {"topology": "net-topo.json", "initial_services": "bg-service.json",
                             "events": f"scripts/script-{script}.json", "traffic": "runtime-traffic.json"}})
        # Anchor common settings inside the run directory so the replay manifest is portable.
        shutil.copyfile(engine / "configs/common.json", output / "native-common.json")
        # The 100-node topology supplies its own pair of parallel incident links.
        selection = {"schema": "resnet-experiment-v1", "extends": "native-common.json", "scenario_dir": "input",
                     "simulation": {"control": PROFILE["control"]}, "cases": [{"case_id": "packet_control", "mode":"control","start_s":0,"end_s":None}]}
        write_json(output / "native-experiment.json", selection)
        scenario, config, _, identity = load_config(output / "native-experiment.json", "packet_control")
    if len(scenario.nodes) != PROFILE["nodes"] or len(scenario.links) != PROFILE["links"]:
        raise ValueError(f"Wrong physical topology: {len(scenario.nodes)} nodes and {len(scenario.links)} links")
    return scenario, config, identity, local
