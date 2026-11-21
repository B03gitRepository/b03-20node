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
FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def merged(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def read_manifest(path, ancestors=()):
    path = path.resolve(strict=True)
    if path in ancestors:
        raise ValueError("Component manifest inheritance cycle")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in " + path.name)
    parent = value.pop("extends", None)
    # Resolve the knowledge path against the manifest that declares it.
    knowledge = value.get("knowledge", {})
    if isinstance(knowledge, dict) and "source" in knowledge:
        knowledge["source"] = str((path.parent / knowledge["source"]).resolve(strict=True))
    return merged(read_manifest(path.parent / parent, (*ancestors, path)), value) if parent else value


def tokens(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(i, str) and i.strip() for i in value):
        raise ValueError("Expected a nonempty symbol list for " + name)
    if len(value) != len(set(value)):
        raise ValueError("Duplicate action in " + name)
    return value


class ComponentSet:
    def __init__(self, path):
        value = read_manifest(path)
        if value.get("schema") != SCHEMA:
            raise ValueError("Unsupported component interface schema")
        self.catalog = value["catalog"]
        self.symbols = tokens(self.catalog["symbols"], "catalog")
        version = self.catalog["version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Missing catalog version")
        if version == "b03.tools.v1" and tuple(self.symbols) != BUILTIN_CATALOG:
            raise ValueError("The published b03.tools.v1 mapping is immutable")
        self.model, self.rules, self.knowledge = (value[k] for k in ("model", "rules", "knowledge"))
        for part in (self.model, self.rules, self.knowledge):
            if not all(isinstance(part.get(k), str) and part[k].strip() for k in ("id", "version")):
                raise ValueError("Each component requires an ID and a release version")
        for part in (self.model, self.rules):
            if part.get("catalogVersion") != version:
                raise ValueError("Component and action catalog versions differ")
            if part["entrypoint"] != "builtin" and not FACTORY.fullmatch(part["entrypoint"]):
                raise ValueError("Expected a module:factory entry point")
        mapping = tokens(self.model["symbols"], "model mapping")
        if any(i not in self.symbols for i in mapping):
            raise ValueError("The model maps a symbol outside the action catalog")
        if self.model["mode"] not in ("frozen", "online-ppo"):
            raise ValueError("Unsupported model mode")
        self.knowledge_path = Path(self.knowledge["source"])
        self._value = value
        self._identity = None

    @property
    def identity(self):
        if self._identity is not None:
            return deepcopy(self._identity)
        public = deepcopy(self._value)
        public["knowledge"]["source"] = self.knowledge_path.name
        public["knowledge"]["sha256"] = hashlib.sha256(self.knowledge_path.read_bytes()).hexdigest()
        pins = {}
        from importlib.util import find_spec
        for kind in ("model", "rules"):
            entry = getattr(self, kind)["entrypoint"]
            if entry == "builtin":
                continue
            module, _ = entry.split(":")
            spec = find_spec(module)
            if spec is None or not spec.origin or not Path(spec.origin).is_file():
                raise ValueError("Provider has no reproducible module file: " + module)
            pins[kind] = hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
        self._identity = {**public, "providerModuleSha256":pins}
        self._identity["manifestSha256"] = digest(self._identity)
        return deepcopy(self._identity)

    def factory(self, kind):
        if kind not in ("model", "rules"):
            raise ValueError("Unknown provider")
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
