"""Select the native-compatible packet provider and bind its actual engine release."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from component_config import DEFAULT_SIMULATOR
import scene

CONTRACT = DEFAULT_SIMULATOR["contractVersion"]
LOCK_MEMBER = "simulator-lock.json"


def _adapter(registration):
    return {key: registration[key] for key in DEFAULT_SIMULATOR}


def select_engine(components):
    registration = components.simulator
    path = registration.get("provenance", {}).get("assets", {}).get("engineLock")
    blob = Path(path or scene.ROOT / "config/simulator.lock.json").read_bytes()
    lock = scene.validate_engine_lock(json.loads(blob))
    return blob, lock


def runtime_identity(registration, lock, blob):
    return {"schema": "b03.packet-simulator-runtime.v1", "adapter": _adapter(registration),
            "engine": {key: lock[key] for key in ("distribution", "version", "runtimeSha256")},
            "engineLockSha256": hashlib.sha256(blob).hexdigest()}


def archived_engine_lock(run, conditions, files, *, default_lock=None):
    """Validate the portable receipt before optionally comparing the installed engine."""
    recorded = conditions.get("simulatorRuntime")
    registration = (conditions.get("components") or {}).get("simulator")
    if recorded is None:
        if LOCK_MEMBER in files or (registration and
                ("simulation" in conditions or registration.get("entrypoint") != "builtin")):
            raise ValueError("Missing simulator runtime identity")
        original = scene.DEPENDENCY if default_lock is None else default_lock
        if conditions["releaseManifestSha256"] != original["runtimeSha256"]:
            raise ValueError("Unknown historical packet simulator release")
        return original
    if LOCK_MEMBER not in files or not registration or registration.get("contractVersion") != CONTRACT:
        raise ValueError("Missing simulator lock or incompatible adapter in the archived run")
    blob = (Path(run) / LOCK_MEMBER).read_bytes()
    lock = scene.validate_engine_lock(json.loads(blob))
    if (recorded != runtime_identity(registration, lock, blob)
            or conditions["releaseManifestSha256"] != lock["runtimeSha256"]):
        raise ValueError("Archived simulator identity differs from its actual engine lock")
    proof = conditions["provenance"]["providers"]["simulator"]
    if "engineLock" in registration.get("provenance", {}).get("assets", {}):
        expected = proof.get("assets", {}).get("engineLock", {})
        if expected != {"type": "file", "sha256": recorded["engineLockSha256"]}:
            raise ValueError("Simulator lock differs from the registered provider asset")
    elif conditions["implementationSha256"].get("config/simulator.lock.json") != recorded["engineLockSha256"]:
        raise ValueError("Simulator lock differs from the registered checkout default")
    return lock


def simulation_type(components):
    """Compose the host's measurement hooks with the selected native packet implementation."""
    from packet_measure import MeasuredSimulation, MeasurementMixin
    from resnet.simulation.simulation import Simulation

    registration = components.simulator
    settings = registration.get("settings", {})
    if registration.get("contractVersion") != CONTRACT or not isinstance(settings, dict):
        raise ValueError("Unsupported packet simulator interface or settings")
    factory = components.factory("simulator")
    if factory is None:
        if _adapter(registration) != DEFAULT_SIMULATOR or settings:
            raise ValueError("The built-in simulator requires its published identity and empty settings")
        return MeasuredSimulation
    implementation = factory(deepcopy(settings))
    if (not isinstance(implementation, type) or not issubclass(implementation, Simulation)
            or issubclass(implementation, MeasurementMixin)):
        raise ValueError("The simulator factory must return a native Simulation subclass without the host meter")
    identity = (getattr(implementation, "adapter_id", None), getattr(implementation, "adapter_version", None),
                getattr(implementation, "contract_version", None))
    if identity != (registration["id"], registration["version"], CONTRACT):
        raise ValueError("The simulator implementation identity differs from its component registration")

    class MeasuredProvider(MeasurementMixin, implementation):
        """Run the provider's physical transitions with the shared business meter."""

    return MeasuredProvider
