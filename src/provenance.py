"""Identify the implementation actually selected by a packet run.

A source verifier looks up the installed distributions without running a provider factory.
The ordinary receipt verifier never calls the environment collection functions.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from importlib import machinery, metadata, util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import urlsplit
from urllib.request import url2pathname

from component_config import digest
from scene import sha256

SCHEMA = "b03.provenance.v2"
COMPONENT_KINDS = ("model", "rules", "knowledge", "simulator")
IGNORE_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def active_kinds(policy, manifest=None):
    if policy == "knowledge":
        return ("knowledge",)
    workorder = policy not in ("package", "single")
    selected = ("model", "rules") if policy == "model" else (("rules",) if workorder else ())
    if manifest and "simulator" in manifest:
        return (*selected, "knowledge", "simulator") if workorder else ("simulator",)
    return selected


def changed(expected, actual, prefix=""):
    """Report paths and both values, including individual files in a distribution."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key in sorted(expected.keys() | actual.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in actual:
                differences.append(f"{path}: missing")
            elif key not in expected:
                differences.append(f"{path}: added")
            else:
                differences.extend(changed(expected[key], actual[key], path))
        return differences
    if expected != actual or type(expected) is not type(actual):
        return [f"{prefix}: recorded {expected!r}, current {actual!r}"]
    return []


def tree(path):
    """Fingerprint a relocatable file or source/data directory without recording its location."""
    path = Path(path).expanduser().resolve(strict=True)
    if path.is_file():
        return {"type": "file", "sha256": sha256(path)}
    if not path.is_dir():
        raise ValueError(f"Expected a file or directory: {path.name}")
    files = {}
    for folder, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        directory = Path(folder)
        for name in dirs:
            if (directory / name).is_symlink():
                raise ValueError(f"Use a regular directory for the linked provenance subtree: {directory / name}")
        for name in sorted(names):
            entry = directory / name
            if not entry.is_file():
                raise ValueError(f"Missing or unsupported provenance member: {entry}")
            files[entry.relative_to(path).as_posix()] = sha256(entry)
    return {"type": "directory", "files": files}


def module_file(module):
    try:
        spec = util.find_spec(module)
    except (ModuleNotFoundError, ImportError, AttributeError, ValueError) as exc:
        raise ValueError(f"Cannot locate provider module {module}: {exc}") from exc
    if spec is None or not spec.origin or not Path(spec.origin).is_file():
        raise ValueError(f"Cannot locate a reproducible module file for {module}")
    return Path(spec.origin).resolve()


def local_module(name):
    """Capture the whole top-level package, including sibling modules and package data."""
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError("provenance.localModules must name importable top-level packages or modules")
    spec = util.find_spec(name)
    if spec is None:
        raise ValueError(f"Missing declared local module: {name}")
    locations = list(spec.submodule_search_locations or ())
    if len(locations) > 1:
        raise ValueError(f"Local namespace {name} spans multiple directories; install its providers as wheels")
    return tree(locations[0] if locations else module_file(name))


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def editable_source(distribution):
    value = json.loads(distribution.read_text("direct_url.json") or "{}")
    if not value.get("dir_info", {}).get("editable"):
        return None
    url = urlsplit(value["url"])
    if url.scheme != "file" or url.netloc not in ("", "localhost"):
        raise ValueError(f"Unsupported editable origin for {distribution.metadata['Name']}; install a wheel")
    return Path(url2pathname(url.path)).resolve(strict=True)


def package_roots(distribution):
    top = (distribution.read_text("top_level.txt") or "").split()
    if top:
        return set(top)
    roots = set()
    for item in distribution.files or ():
        first = item.parts[0]
        name = first if len(item.parts) > 1 else first.split(".")[0]
        if name.isidentifier() and not name.startswith("__editable__") and (
            len(item.parts) > 1 or first.endswith((".py", ".so", ".pyd"))
        ):
            roots.add(name)
    return roots


def check_namespace_children(name, root, locations, owned):
    """With multiple namespace portions, check which portion supplies each importable child."""
    children = defaultdict(set)
    for path in owned:
        if not path.is_relative_to(root):
            continue
        relative = path.relative_to(root)
        filename = relative.name
        suffix = next((s for s in machinery.all_suffixes() if filename.endswith(s)), None)
        if not suffix:
            continue
        stem = filename[:-len(suffix)]
        parts = relative.parts[:-1] + ((stem,) if stem != "__init__" else ())
        if parts and all(part.isidentifier() for part in parts):
            children[parts[0]].add(path)
    for child, paths in children.items():
        fullname = name + "." + child
        # The explicit parent search path is already resolved. A bare child name lets the
        # finder describe a nested namespace without requiring its parent in sys.modules.
        spec = machinery.PathFinder.find_spec(child, locations)
        if spec is None:
            continue
        sublocations = list(spec.submodule_search_locations or ())
        child_root = root / child
        if sublocations:
            if child_root not in {Path(p).resolve() for p in sublocations}:
                raise ValueError(f"Installed namespace dependency is shadowed at import {fullname}: {sublocations}")
            if len(sublocations) > 1 and spec.origin is None:
                check_namespace_children(fullname, child_root, sublocations, paths)
        elif not spec.origin or Path(spec.origin).resolve() not in paths:
            raise ValueError(f"Installed namespace dependency is shadowed at import {fullname}: {spec.origin}")


def supporting_files(location, installed, module, hash_file):
    """Include importable files left beside a wheel by a previous install or local tooling."""
    extra = {}
    for folder, dirs, names in os.walk(location):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        for name in names:
            path = Path(folder) / name
            if path.is_file() and path.resolve() not in installed:
                extra[module + "/" + path.relative_to(location).as_posix()] = hash_file(path.resolve())
    return extra


def distribution_record(distribution, installed):
    """Read the installed bytes, including the contents of native modules and package data."""
    name = distribution.metadata["Name"]
    editable = editable_source(distribution)
    editable_modules = installed.local_roots
    if editable and not editable_modules:
        raise ValueError(f"Editable distribution {name} needs provenance.localModules for all its source roots, or a wheel")
    if not distribution.files:
        raise ValueError(f"Distribution {name} has no installed file inventory; install a wheel")
    files = {}
    live = set()
    for item in distribution.files:
        label = item.as_posix()
        if item.is_absolute():
            raise ValueError(f"Invalid absolute installed file in {name}: {label}")
        if "__pycache__" in item.parts:
            continue
        in_metadata = any(p.endswith((".dist-info", ".egg-info")) for p in item.parts[:-1])
        # These installer bookkeeping files contain machine-local URLs, generated file lists
        # and cache entries. The effective source and metadata are read independently below.
        if in_metadata and item.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}:
            continue
        if editable and not in_metadata and (item.suffix == ".pth" or item.name.startswith("__editable__")):
            continue
        path = Path(distribution.locate_file(item))
        if not path.is_file():
            raise ValueError(f"Missing installed file in {name}: {label}")
        effective_path = path.resolve()
        files[label] = installed.hash_file(effective_path)
        live.add(effective_path)
    modules = {}
    additions = {}
    shared_initializers = {}
    roots = package_roots(distribution)
    if editable:
        for module in sorted(roots | set(editable_modules)):
            spec = util.find_spec(module)
            if spec is None:
                raise ValueError(f"Cannot resolve editable module {name}/{module}")
            locations = list(spec.submodule_search_locations or ()) or [module_file(module)]
            if not all(Path(p).resolve().is_relative_to(editable) for p in locations):
                if module in roots:
                    raise ValueError(f"Editable import origin differs for {name}/{module}")
                continue
            modules[module] = local_module(module)
        if not modules:
            raise ValueError(f"No declared modules resolve inside editable distribution {name}")
    else:
        for module in sorted(roots):
            spec = util.find_spec(module)
            if spec is None:
                continue
            locations = list(spec.submodule_search_locations or ())
            origin = Path(spec.origin).resolve() if spec.origin not in (None, "built-in", "frozen") else None
            if not locations:
                if origin not in live:
                    raise ValueError(f"Installed dependency {name} is shadowed at import {module}: {spec.origin}")
                continue
            root = Path(distribution.locate_file(module)).resolve()
            if root not in {Path(p).resolve() for p in locations}:
                raise ValueError(f"Installed dependency {name} is shadowed at import {module}: {locations}")
            claimed = installed.package_files(module, name, live)
            if origin is not None and origin not in live:
                # Multiple wheels may fill different subpackages of the same regular package.
                # Its effective initializer must be installed in this package by a contributor.
                if origin.parent != root or origin not in claimed:
                    raise ValueError(f"Installed dependency {name} is shadowed at import {module}: {origin}")
                shared_initializers[module + "/" + origin.name] = installed.hash_file(origin)
            if len(locations) > 1 and origin is None:
                check_namespace_children(module, root, locations, live)
            additions.update(installed.additional_files(module, root, claimed))
    result = {"name": name, "version": distribution.version, "files": files}
    if editable:
        result["editableModules"] = modules
    if shared_initializers:
        result["sharedInitializers"] = shared_initializers
    if not editable:
        result["additionalImportFiles"] = additions
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    wheel_hash = direct.get("archive_info", {}).get("hashes", {}).get("sha256")
    if wheel_hash and urlsplit(direct.get("url", "")).path.lower().endswith(".whl"):
        result["installationWheelSha256"] = wheel_hash
    return result


def requirement_parser():
    try:
        from packaging.requirements import Requirement
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import Requirement
        except ImportError as exc:
            raise ValueError("Install packaging to resolve distribution provenance") from exc
    return Requirement


class Distributions:
    def __init__(self):
        self.records = {}
        self.extras = defaultdict(set)
        self._owners = None
        self.local_roots = set()
        self._shared_files = {}
        self._additional_files = {}
        self._hashes = {}

    def hash_file(self, path):
        # This cache lives for one collection. Strict verification starts a fresh collection.
        if path not in self._hashes:
            self._hashes[path] = sha256(path)
        return self._hashes[path]

    def owners(self, top):
        if self._owners is None:
            self._owners = metadata.packages_distributions()
        return set(self._owners.get(top, ()))

    def package_files(self, module, name, live):
        names = self.owners(module) | {name}
        if len({canonical_name(n) for n in names}) == 1:
            return live
        if module not in self._shared_files:
            claimed = set()
            # A venv can expose multiple site-packages roots. Use the real location of every
            # RECORD member so a namespace contributor cannot satisfy a different directory.
            for owner in names:
                for dist in metadata.distributions(name=owner):
                    for item in dist.files or ():
                        if item.parts[0] == module and "__pycache__" not in item.parts:
                            claimed.add(Path(dist.locate_file(item)).resolve())
            self._shared_files[module] = claimed
        return self._shared_files[module]

    def additional_files(self, module, root, claimed):
        key = (module, root)
        if key not in self._additional_files:
            self._additional_files[key] = supporting_files(root, claimed, module, self.hash_file)
        return self._additional_files[key]

    def owner(self, module, origin, declared):
        names = list(self.owners(module.split(".")[0]))
        if declared:
            names.insert(0, declared)
        matches = set()
        for name in set(names):
            try:
                d = metadata.distribution(name)
            except metadata.PackageNotFoundError as exc:
                raise ValueError(f"Missing distribution {name} for provider {module}") from exc
            editable = editable_source(d)
            if editable and origin.is_relative_to(editable):
                matches.add(d.metadata["Name"])
            elif any(Path(d.locate_file(i)).resolve() == origin for i in d.files or ()
                     if i.name == origin.name):
                matches.add(d.metadata["Name"])
        if declared:
            for match in matches:
                if canonical_name(match) == canonical_name(declared):
                    return match
            raise ValueError(f"Distribution {declared} does not own the imported provider {module}")
        if len(matches) > 1:
            raise ValueError(f"Several distributions claim {module}; select provenance.distribution from {sorted(matches)}")
        return next(iter(matches), None)

    def collect(self, requested):
        if not requested:
            return {}
        Requirement = requirement_parser()
        queue = [(value, {""}, "selection") for value in requested]
        while queue:
            value, parent_extras, parent = queue.pop()
            req = Requirement(value)
            if req.marker and not any(req.marker.evaluate({"extra": extra}) for extra in parent_extras):
                continue
            name = canonical_name(req.name)
            try:
                dist = metadata.distribution(req.name)
            except metadata.PackageNotFoundError as exc:
                raise ValueError(f"Missing distribution {req.name} required by {parent}") from exc
            # A published requirement is part of the trace. Reject an environment failing it.
            if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
                raise ValueError(f"{parent} requires {req}, installed {dist.metadata['Name']} {dist.version}")
            extras = {""} | {canonical_name(extra) for extra in req.extras}
            if name in self.records and extras <= self.extras[name]:
                continue
            self.extras[name].update(extras)
            if name not in self.records:
                self.records[name] = distribution_record(dist, self)
            queue.extend((child, self.extras[name], name) for child in dist.requires or ())
        return self.records


def active_registration(components, policy):
    result = {}
    if active_kinds(policy):
        result["catalog"] = deepcopy(components.catalog)
    for kind in active_kinds(policy, components._value):
        reg = deepcopy(getattr(components, kind))
        if kind == "knowledge":
            reg["source"] = components.knowledge_path.name
            reg["sha256"] = sha256(components.knowledge_path)
        config = reg.get("provenance", {})
        if "assets" in config:
            # A logical asset name is portable. The bytes below identify its content.
            config["assets"] = sorted(config["assets"])
        result[kind] = reg
    return result


def provider_records(root, components, policy, distributions, requested):
    providers = {}
    for kind in active_kinds(policy, components._value):
        reg = getattr(components, kind)
        config = reg.get("provenance", {})
        requested.extend(config.get("distributions", ()))
        info = {"assets": {k: tree(v) for k, v in config.get("assets", {}).items()}}
        local = config.get("localModules", [])
        info["localModules"] = {m: local_module(m) for m in local}
        distributions.local_roots.update(local)
        if reg["entrypoint"] != "builtin":
            module = reg["entrypoint"].split(":")[0]
            origin = module_file(module)
            top = module.split(".")[0]
            info["entrySha256"] = sha256(origin)
            if origin.is_relative_to(root / "src") and not config.get("distribution"):
                info["source"] = {"checkout": origin.relative_to(root).as_posix()}
            else:
                owner = distributions.owner(module, origin, config.get("distribution"))
                if owner:
                    info["source"] = {"distribution": canonical_name(owner)}
                    requested.append(owner)
                else:
                    if top not in local:
                        raise ValueError(f"Local provider {module} needs provenance.localModules including {top} and its other local imports, or an installed wheel")
                    info["source"] = {"localModule": top}
        providers[kind] = info
    return providers


def checkout_files(root, native):
    paths = [root / "run.py", root / "config/profile.json", root / "config/simulator.lock.json",
             root / "requirements.txt"]
    if (root / "episode.py").is_file():
        paths.append(root / "episode.py")
    if (root / "train.py").is_file():
        paths.append(root / "train.py")
    if native:
        paths.extend([root / "config/model.json", root / "requirements-model.txt"])
    if (root / "src/b03_formulas.py").is_file():
        paths.append(root / "knowledge/formulas-v1.json")
    # A partner factory may delegate to any published helper, including the examples.
    paths.extend((root / "src").rglob("*.py"))
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(paths)}


def revision(root):
    if not (root / ".git").exists():
        return None
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def public_requirements(root, policy, native):
    if policy in ("package", "single"):
        return ["simpy"]
    paths = [root / "requirements.txt"]
    if native:
        paths.append(root / "requirements-model.txt")
    return [line.strip() for file in paths for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-r "))]


def pinned_files(directory, names, label):
    if directory is None:
        raise ValueError(f"Supply --{label} for strict verification of the original model")
    path = Path(directory).expanduser()
    result = {}
    for name, pin in names.items():
        member = path / name
        if not member.is_file():
            raise ValueError(f"Missing {label} file: {name}")
        actual = sha256(member)
        if actual != pin:
            raise ValueError(f"{label}.{name}: recorded {pin}, current {actual}")
        result[name] = actual
    return result


def model_assets(root, native, policy_identity, checkpoint, tokenizer):
    if not native:
        return {role: tree(path) for role, path in (("checkpoint", checkpoint), ("tokenizer", tokenizer))
                if path is not None}
    spec_file = root / "config/model.json"
    spec = json.loads(spec_file.read_bytes())
    if spec["format"] == "hf-safetensors":
        weights = spec["weightFilesSha256"]
    else:
        version = str(policy_identity["version"])
        meta = spec["nativeVersions"][version]
        weights = {"training.pt": meta["checkpointSha256"], "metadata.json": meta["metadataSha256"]}
    if (policy_identity.get("modelSpecSha256") != sha256(spec_file)
            or policy_identity.get("checkpointFilesSha256") != weights):
        raise ValueError("Native frozen policy receipt differs from the public model specification")
    return {"checkpoint": pinned_files(checkpoint, weights, "checkpoint"),
            "tokenizer": pinned_files(tokenizer or checkpoint, spec["tokenizerSha256"], "tokenizer")}


def capture_provenance(root, components, *, checkpoint=None, tokenizer=None, policy="model",
                       policy_identity=None, runtime=None):
    root = Path(root).resolve()
    policy_identity = policy_identity or {}
    native = policy == "model" and (components.model["entrypoint"] == "builtin"
                                    or policy_identity.get("kind") == "frozen-model")
    requested = public_requirements(root, policy, native) if runtime is None else list(runtime)
    runtime_roots = list(requested)
    packages = Distributions()
    providers = provider_records(root, components, policy, packages, requested)
    assets = model_assets(root, native, policy_identity, checkpoint, tokenizer) if policy == "model" else {}
    knowledge = components.knowledge_path.read_bytes()
    evidence = {
        "schema": SCHEMA, "policy": policy, "nativeFrozen": native,
        "gitCommit": revision(root), "checkoutFiles": checkout_files(root, native),
        "python": {"implementation": platform.python_implementation(), "version": platform.python_version(),
                   "system": platform.system(), "machine": platform.machine()},
        "runtimeRequirements": runtime_roots, "activeComponents": active_registration(components, policy),
        "providers": providers, "distributions": packages.collect(requested), "modelAssets": assets,
        "knowledge": {"id": components.knowledge["id"], "version": components.knowledge["version"],
                      "sha256": hashlib.sha256(knowledge).hexdigest(), "text": knowledge.decode("utf-8")},
    }
    evidence["fingerprint"] = digest(evidence)
    return evidence


def verify_provenance(archived, root, components=None, *, checkpoint=None, tokenizer=None, policy_identity=None):
    from component_config import load_components

    policy = archived["policy"]
    required_assets = archived["modelAssets"]
    if "checkpoint" in required_assets and checkpoint is None:
        return ["modelAssets.checkpoint: supply the original --checkpoint"]
    if "tokenizer" in required_assets and tokenizer is None and not archived["nativeFrozen"]:
        return ["modelAssets.tokenizer: supply the original --tokenizer"]
    try:
        components = components or load_components()
        components.activate(policy)
        result = changed(archived["activeComponents"], active_registration(components, policy), "activeComponents")
        if result:
            return [*result, "Select the component manifest used for this run with --components"]
        actual = capture_provenance(root, components, checkpoint=checkpoint, tokenizer=tokenizer, policy=policy,
                                    policy_identity=policy_identity, runtime=archived["runtimeRequirements"])
    except (ValueError, OSError, KeyError, ImportError) as exc:
        return [str(exc)]
    # Raw knowledge bytes have a separately checked digest. Do not print their complete text.
    expected = {k: v for k, v in archived.items() if k not in ("fingerprint", "knowledge", "gitCommit")}
    current = {k: v for k, v in actual.items() if k not in ("fingerprint", "knowledge", "gitCommit")}
    # Local wheel URLs and archive availability depend on how the same installed files were obtained.
    for detail in (expected, current):
        detail["distributions"] = {
            name: {k: v for k, v in record.items() if k != "installationWheelSha256"}
            for name, record in detail["distributions"].items()
        }
    result = changed(expected, current)
    result.extend(changed({k: v for k, v in archived["knowledge"].items() if k != "text"},
                          {k: v for k, v in actual["knowledge"].items() if k != "text"}, "knowledge"))
    return result
