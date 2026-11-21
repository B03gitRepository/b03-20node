"""Versioned selection of the model, rule solver, and read-only technical knowledge."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import re

from scene import ROOT

SCHEMA = "b03.components.v1"
BUILTIN_CATALOG = tuple("ABCDEFGHIJKL")
DEFAULT = ROOT / "config/components.json"
PROVIDERS = ("model", "rules", "simulator", "knowledge")
DEFAULT_SIMULATOR = {"id": "b03.resnet", "version": "1.1.0",
                     "contractVersion": "b03.packet-simulator.v1", "entrypoint": "builtin"}
FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def work_order_components_digest(components):
    fields = ("catalog", "model", "rules", "providerModuleSha256")
    return digest({key: components[key] for key in fields})


def merged(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def read_manifest(path, ancestors=(), *, inputs=None, allow_missing=False):
    path = path.resolve(strict=not allow_missing)
    if inputs is not None:
        inputs.add(path)
    if allow_missing and not path.exists():
        return {}
    if path in ancestors:
        raise ValueError("Component manifest inheritance cycle")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in " + path.name)
    parent = value.pop("extends", None)
    # Resolve the knowledge path against the manifest that declares it.
    knowledge = value.get("knowledge", {})
    if isinstance(knowledge, dict) and "source" in knowledge:
        source = (path.parent / knowledge["source"]).resolve(strict=not allow_missing)
        knowledge["source"] = str(source)
        if inputs is not None:
            inputs.add(source)
    # Each asset is relative to the manifest that introduced it, including inherited entries.
    for kind in PROVIDERS:
        part = value.get(kind, {})
        registration = part.get("provenance", {}) if isinstance(part, dict) else {}
        if not isinstance(registration, dict):
            raise ValueError("Expected a provenance object for " + kind)
        if "assets" in registration:
            assets = registration["assets"]
            if not isinstance(assets, dict) or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in assets.items()
            ):
                raise ValueError("provenance.assets maps nonempty logical names to file or directory paths")
            registration["assets"] = {k: str((path.parent / v).resolve()) for k, v in assets.items()}
            if inputs is not None:
                inputs.update(map(Path, registration["assets"].values()))
    if parent:
        base = read_manifest(path.parent / parent, (*ancestors, path), inputs=inputs, allow_missing=allow_missing)
        return merged(base, value)
    return value


def tokens(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(i, str) and i.strip() for i in value):
        raise ValueError("Expected a nonempty symbol list for " + name)
    if len(value) != len(set(value)):
        raise ValueError("Duplicate action in " + name)
    return value


class ComponentSet:
    def __init__(self, path):
        self.input_paths = set()
        value = read_manifest(path, inputs=self.input_paths)
        if value.get("schema") != SCHEMA:
            raise ValueError("Unsupported component interface schema")
        self.catalog = value["catalog"]
        self.symbols = tokens(self.catalog["symbols"], "catalog")
        version = self.catalog["version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Missing catalog version")
        if version == "b03.tools.v1" and tuple(self.symbols) != BUILTIN_CATALOG:
            raise ValueError("The published b03.tools.v1 mapping is immutable")
        self.simulator = value.setdefault("simulator", dict(DEFAULT_SIMULATOR))
        self.model, self.rules, self.knowledge = (value[k] for k in ("model", "rules", "knowledge"))
        self.knowledge.setdefault("entrypoint", "builtin")
        for part in (self.model, self.rules, self.simulator, self.knowledge):
            if not all(isinstance(part.get(k), str) and part[k].strip() for k in ("id", "version")):
                raise ValueError("Each component requires an ID and a release version")
        for part in (self.model, self.rules):
            if part.get("catalogVersion") != version:
                raise ValueError("Component and action catalog versions differ")
        for part in (self.model, self.rules, self.simulator, self.knowledge):
            if not isinstance(part.get("entrypoint"), str) or (part["entrypoint"] != "builtin" and not FACTORY.fullmatch(part["entrypoint"])):
                raise ValueError("Expected a module:factory entry point")
            registration = part.get("provenance", {})
            unknown = registration.keys() - {"distribution", "distributions", "localModules", "assets"}
            if unknown:
                raise ValueError("Unknown provenance fields: " + ", ".join(sorted(unknown)))
            if "distribution" in registration and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(registration["distribution"])):
                raise ValueError("provenance.distribution must be an installed distribution name")
            for field in ("distributions", "localModules"):
                items = registration.get(field, [])
                if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
                    raise ValueError("Expected a string list for provenance." + field)
                if len(set(items)) != len(items):
                    raise ValueError("Duplicate entry in provenance." + field)
        mapping = tokens(self.model["symbols"], "model mapping")
        if any(i not in self.symbols for i in mapping):
            raise ValueError("The model maps a symbol outside the action catalog")
        if self.model["mode"] not in ("frozen", "online-ppo"):
            raise ValueError("Unsupported model mode")
        self.knowledge_path = Path(self.knowledge["source"])
        self._value = value
        self._identity = None
        self._active = PROVIDERS

    def activate(self, policy):
        """Only providers reachable in this run need an installed entry point."""
        from provenance import active_kinds
        active = active_kinds(policy, self._value)
        if self._active != active:
            self._active = active
            self._identity = None
        return self

    @property
    def identity(self):
        if self._identity is not None:
            return deepcopy(self._identity)
        public = deepcopy(self._value)
        public["knowledge"]["source"] = self.knowledge_path.name
        public["knowledge"]["sha256"] = hashlib.sha256(self.knowledge_path.read_bytes()).hexdigest()
        pins = {}
        from provenance import module_file
        for kind in PROVIDERS:
            registration = public[kind].get("provenance", {})
            if "assets" in registration:
                registration["assets"] = {k: Path(v).name for k, v in registration["assets"].items()}
        for kind in self._active:
            entry = getattr(self, kind)["entrypoint"]
            if entry == "builtin":
                continue
            module, _ = entry.split(":")
            pins[kind] = hashlib.sha256(module_file(module).read_bytes()).hexdigest()
        self._identity = {**public, "providerModuleSha256":pins}
        self._identity["manifestSha256"] = digest(self._identity)
        return deepcopy(self._identity)

    def factory(self, kind):
        if kind not in self._active:
            raise ValueError("Unselected provider: " + kind)
        entrypoint = getattr(self, kind)["entrypoint"]
        if entrypoint == "builtin":
            return None
        module, attr = entrypoint.split(":")
        loaded = importlib.import_module(module)
        if hashlib.sha256(Path(loaded.__file__).read_bytes()).hexdigest() != self.identity["providerModuleSha256"][kind]:
            raise ValueError("The provider module changed after configuration loading")
        factory = getattr(loaded, attr)
        if not callable(factory):
            raise ValueError("Configured provider is not callable: " + entrypoint)
        return factory


def load_components(path=None):
    return ComponentSet(Path(path) if path else DEFAULT)
