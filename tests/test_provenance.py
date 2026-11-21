"""Exercise real local wheels, editable providers, model assets and sealed legacy evidence."""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from component_config import digest, load_components
from component_examples import examples, knowledge_example
from integrity import check_archive, check_recorded, seal
from provenance import Distributions, capture_provenance, model_assets, verify_provenance
from scene import sha256


def dump(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def wheel(folder, name, payload, requires=(), top=None):
    """Build a standard pure-Python wheel, with a complete PEP 427 RECORD."""
    stem = name.replace("-", "_")
    prefix = stem + "-1.0.dist-info/"
    content = dict(payload)
    content[prefix + "WHEEL"] = "Wheel-Version: 1.0\nGenerator: b03-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    content[prefix + "METADATA"] = (f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
                                    + "".join(f"Requires-Dist: {s}\n" for s in requires))
    content[prefix + "top_level.txt"] = "\n".join(top or [stem]) + "\n"
    record = StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for path, text in content.items():
        raw = text.encode()
        pin = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        writer.writerow((path, "sha256=" + pin, len(raw)))
    writer.writerow((prefix + "RECORD", "", ""))
    content[prefix + "RECORD"] = record.getvalue()
    path = folder / f"{stem}-1.0-py3-none-any.whl"
    with ZipFile(path, "w") as bundle:
        for entry, text in content.items():
            bundle.writestr(entry, text)
    return path


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.workspace.cleanup)
        base = Path(cls.workspace.name)
        cls.installed = base / "installed"
        provider = wheel(base, "b03-fixture-actor", {
            "b03_fixture_actor/__init__.py": "",
            "b03_fixture_actor/entry.py": "from .helper import choice\n\ndef build(**kwargs):\n    return choice\n",
            "b03_fixture_actor/helper.py": "from b03_fixture_math import choose\nchoice = choose\n",
            "b03_fixture_actor/resource.dat": "scale=3\n",
        }, ("b03-fixture-math==1.0", "not-available-b03-fixture; python_version < '2'"))
        helper = wheel(base, "b03-fixture-math", {
            "b03_fixture_math/__init__.py": "from .ops import choose\n",
            "b03_fixture_math/ops.py": "def choose(observation):\n    return observation[0]\n",
        })
        extra = wheel(base, "b03-fixture-extra", {"b03_fixture_extra/__init__.py": "scale = 3\n"})
        common = wheel(base, "b03-fixture-common", {
            "b03_fixture_space/__init__.py": "# installed shared initializer\n",
            "b03_fixture_space/regular.py": "value = 17\n",
            "b03_fixture_ns/first/__init__.py": "value = 2\n",
        }, top=["b03_fixture_space", "b03_fixture_ns"])
        addon = wheel(base, "b03-fixture-addon", {
            "b03_fixture_space/addon/payload.bin": "data-only contribution",
            "b03_fixture_ns/second/__init__.py": "value = 3\n",
            "b03_fixture_ns/multi/branch/__init__.py": "value = 4\n",
        }, top=["b03_fixture_space", "b03_fixture_ns"])
        subprocess.run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
                        "install", "--no-index", "--no-deps", "--no-compile", "--target", str(cls.installed),
                        str(provider), str(helper), str(extra), str(common), str(addon)], check=True, capture_output=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.site = self.folder / "site"
        shutil.copytree(self.installed, self.site)
        self.add_path(self.site)
        self.root = self.folder / "checkout"
        for name in ("src", "config", "knowledge"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("run.py", "requirements.txt", "requirements-model.txt"):
            shutil.copy(ROOT / name, self.root / name)
        self.checkpoint = self.folder / "weights"
        self.checkpoint.mkdir()
        (self.checkpoint / "part-0.bin").write_bytes(b"first")
        (self.checkpoint / "part-1.bin").write_bytes(b"second")
        self.tokenizer = self.folder / "tokens.model"
        self.tokenizer.write_bytes(b"tokens")
        self.manifest = self.folder / "experiment.json"
        self.registration = {
            "id": "partner.actor", "entrypoint": "b03_fixture_actor.entry:build",
            "provenance": {"distribution": "b03-fixture-actor", "distributions": ["b03-fixture-extra==1.0"]},
        }

    def add_path(self, path):
        sys.path.insert(0, str(path))
        importlib.invalidate_caches()
        def restore():
            sys.path.remove(str(path))
            for name in tuple(sys.modules):
                if name.startswith("b03_fixture_"):
                    del sys.modules[name]
            importlib.invalidate_caches()
        self.addCleanup(restore)

    def components(self, **overrides):
        dump(self.manifest, {"extends": str(self.root / "config/components.json"), **overrides})
        return load_components(self.manifest)

    def capture(self, components, **options):
        args = dict(checkpoint=self.checkpoint, tokenizer=self.tokenizer, policy="model", runtime=())
        return capture_provenance(self.root, components, **{**args, **options})

    def strict(self, archive, components, **options):
        args = dict(checkpoint=self.checkpoint, tokenizer=self.tokenizer)
        return verify_provenance(archive, self.root, components, **{**args, **options})

    def test_installed_wheel_dependency_and_custom_weight_bytes(self):
        components = self.components(model=self.registration)
        archive = self.capture(components)
        self.assertEqual(set(archive["distributions"]), {"b03-fixture-actor", "b03-fixture-math", "b03-fixture-extra"})
        self.assertRegex(archive["distributions"]["b03-fixture-actor"]["installationWheelSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.strict(archive, components), [])
        self.assertIn("--checkpoint", "\n".join(self.strict(archive, components, checkpoint=None)))
        self.assertEqual(components.factory("model")(config={})(["C"]), "C")
        changes = [
            self.site / "b03_fixture_actor/helper.py", self.site / "b03_fixture_actor/resource.dat",
            self.site / "b03_fixture_math/ops.py", self.site / "b03_fixture_extra/__init__.py",
            self.checkpoint / "part-1.bin", self.tokenizer,
        ]
        for path in changes:
            with self.subTest(path=path.name):
                previous = path.read_bytes()
                path.write_bytes(previous + b"\n")
                errors = self.strict(archive, components)
                self.assertIn("modelAssets.tokenizer" if path == self.tokenizer else path.name, "\n".join(errors))
                path.write_bytes(previous)
        self.assertEqual(self.strict(archive, components), [])
        (self.site / "b03_fixture_actor/unlisted.py").write_text("override = 1")
        self.assertIn("b03_fixture_actor/unlisted.py", "\n".join(self.strict(archive, components)))

    def test_shared_regular_and_namespace_packages_hash_each_file_once(self):
        # The addon ships no initializer for the regular package, as with several NVIDIA wheels.
        reads = []
        def count(path):
            reads.append(path)
            return sha256(path)
        with patch("provenance.sha256", side_effect=count):
            result = Distributions().collect(["b03-fixture-common", "b03-fixture-addon"])
        common, addon = result["b03-fixture-common"], result["b03-fixture-addon"]
        key = "b03_fixture_space/__init__.py"
        self.assertEqual(addon["sharedInitializers"][key], common["files"][key])
        self.assertEqual(common["additionalImportFiles"], {})
        self.assertEqual(addon["additionalImportFiles"], {})
        self.assertIn("b03_fixture_space/addon/payload.bin", addon["files"])
        self.assertNotIn("b03_fixture_space/addon/payload.bin", common["files"])
        self.assertEqual(reads.count((self.site / key).resolve()), 1)
        self.assertEqual(len(reads), len(set(reads)))
        (self.site / key).write_text("# changed shared initializer\n", encoding="utf-8")
        newer = Distributions().collect(["b03-fixture-addon"])
        self.assertNotEqual(newer["b03-fixture-addon"]["sharedInitializers"], addon["sharedInitializers"])

    def test_foreign_regular_root_and_namespace_child_still_fail(self):
        before = self.folder / "earlier-on-path"
        root = before / "b03_fixture_space"
        root.mkdir(parents=True)
        (root / "__init__.py").write_text("hidden = True\n")
        self.add_path(before)
        with self.assertRaisesRegex(ValueError, "shadowed at import b03_fixture_space"):
            Distributions().collect(["b03-fixture-addon"])
        (root / "__init__.py").unlink()
        (before / "b03_fixture_ns/multi/other").mkdir(parents=True)
        importlib.invalidate_caches()
        self.assertIn("b03-fixture-addon", Distributions().collect(["b03-fixture-addon"]))
        # An earlier implicit top-level namespace can also hide an actual installed child.
        ns = before / "b03_fixture_ns/second"
        ns.mkdir(parents=True)
        (ns / "__init__.py").write_text("hidden = True\n")
        importlib.invalidate_caches()
        with self.assertRaisesRegex(ValueError, "shadowed at import b03_fixture_ns.second"):
            Distributions().collect(["b03-fixture-addon"])

    def test_inactive_files_and_missing_unselected_model(self):
        model = dict(self.registration, entrypoint="b03_fixture_missing:build")
        components = self.components(model=model).activate("I")
        self.assertEqual(components.identity["providerModuleSha256"], {})
        archive = self.capture(components, policy="I", checkpoint=self.folder / "nonexistent-weights")
        for relative in ("knowledge/technical-v2.json", "config/example-model.json"):
            (self.root / relative).write_text("changed inactive file", encoding="utf-8")
        self.assertEqual(archive["modelAssets"], {})
        self.assertEqual(archive["distributions"], {})
        self.assertEqual(self.strict(archive, components, checkpoint=None, tokenizer=None), [])
        archive["gitCommit"] = "0" * 40
        archive["fingerprint"] = digest({k: v for k, v in archive.items() if k != "fingerprint"})
        self.assertEqual(self.strict(archive, components), [])
        (self.root / "src/example_rules.py").write_text("changed helper available to partner factories", encoding="utf-8")
        self.assertIn("src/example_rules.py", "\n".join(self.strict(archive, components)))

    def test_declared_local_module_and_resource(self):
        package = self.site / "b03_fixture_local"
        package.mkdir()
        (package / "__init__.py").write_text("from .entry import build\n")
        (package / "entry.py").write_text("def build(**kwargs):\n    pass\n")
        asset = self.folder / "coefficients.bin"
        asset.write_bytes(b"model-data")
        registration = {"id": "partner.local", "entrypoint": "b03_fixture_local.entry:build"}
        with self.assertRaisesRegex(ValueError, "localModules"):
            self.capture(self.components(model=registration))
        registration["provenance"] = {"localModules": ["b03_fixture_local"], "assets": {"table": asset.name}}
        components = self.components(model=registration)
        archive = self.capture(components)
        self.assertNotIn(str(self.folder), json.dumps(archive))
        (package / "extra.dat").write_bytes(b"new config")
        asset.write_bytes(b"changed")
        errors = "\n".join(self.strict(archive, components))
        self.assertIn("extra.dat", errors)
        self.assertIn("providers.model.assets.table", errors)

    def test_editable_package_checks_working_tree(self):
        source = self.folder / "editable"
        package = source / "src/b03_fixture_editable"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        helper = package / "helper.py"
        helper.write_text("weight = 1\n")
        (package / "entry.py").write_text("from .helper import weight\n")
        self.add_path(source / "src")
        info = self.site / "b03_fixture_editable-2.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: b03-fixture-editable\nVersion: 2.0\n")
        (info / "top_level.txt").write_text("b03_fixture_editable\n")
        dump(info / "direct_url.json", {"url": source.as_uri(), "dir_info": {"editable": True}})
        (info / "RECORD").write_text("".join(f"{info.name}/{name},,\n" for name in
                                            ("METADATA", "top_level.txt", "direct_url.json", "RECORD")))
        registration = {"entrypoint": "b03_fixture_editable.entry:build",
                        "provenance": {"distribution": "b03-fixture-editable"}}
        with self.assertRaisesRegex(ValueError, "Editable distribution"):
            self.capture(self.components(model=registration))
        registration["provenance"]["localModules"] = ["b03_fixture_editable"]
        components = self.components(model=registration)
        archive = self.capture(components)
        self.assertIn("helper.py", archive["distributions"]["b03-fixture-editable"]["editableModules"]
                      ["b03_fixture_editable"]["files"])
        helper.write_text("weight = 2\n")
        self.assertIn("helper.py", "\n".join(self.strict(archive, components)))

    def conditions(self, components, proof, raw):
        return {
            "mode": "workorder", "rcPolicy": {"kind": "model-adapter"}, "script": "09",
            "nodeCount": 20, "linkCount": 30, "scene": "fixture/script-09", "releaseManifestSha256": "runtime",
            "sourceHashes": {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()},
            "components": components.identity, "implementationSha256": proof["checkoutFiles"], "provenance": proof,
        }

    def test_sealed_archive_after_removing_provider_and_weights(self):
        components = self.components(model=self.registration).activate("model")
        archive = self.capture(components)
        raw = {"net-topo.json": b"topology", "bg-service.json": b"demand", "scripts/script-09.json": b"script"}
        conditions = self.conditions(components, archive, raw)
        trace = {"event": "b03_rc_plan", "work_id": "w1", "time_ns": 7,
                 "tool_choice": {"modelCalled": True, "selected": "C", "observation": {"legalActions": ["C"]}},
                 "component_io": {"action": "C", "output": {"target": "route"}}}
        run = self.folder / "run"
        (run / "input/scripts").mkdir(parents=True)
        for name, content in raw.items():
            (run / "input" / name).write_bytes(content)
        overlay = {"schema": "b03.demand-ends.v1", "ends": {"BG1": 42}}
        conditions["activityOverlay"] = overlay
        dump(run / "CONDITIONS.json", conditions)
        dump(run / "input/b03-demand-ends.json", overlay)
        dump(run / "SUMMARY.json", {})
        dump(run / "b03.jsonl", trace)
        (run / "business-intervals.jsonl").write_bytes(b"")
        seal(run)
        aligned_profile = {"nodes": 20, "links": 30, "officialScenario": "fixture", "pins": conditions["sourceHashes"]}
        engine = self.folder / "engine"
        engine.mkdir()
        source_file = engine / "clock.py"
        source_file.write_bytes(b"native clock")
        lock = {"runtimeSha256": "runtime", "files": {"clock.py": sha256(source_file)}}
        strict_args = dict(strict_source=True, components=components, checkpoint=self.checkpoint, tokenizer=self.tokenizer)
        with patch("integrity.ROOT", self.root), patch("integrity.PROFILE", aligned_profile),                 patch("integrity.DEPENDENCY", lock), patch("scene.DEPENDENCY", lock):
            with patch.dict(os.environ, {"B03_SIMULATOR": ""}):
                self.assertEqual(check_archive(run, conditions, **strict_args)["simulatorCheck"], "release-lock-only")
            with patch.dict(os.environ, {"B03_SIMULATOR": str(engine)}):
                self.assertEqual(check_archive(run, conditions, **strict_args)["simulatorCheck"], "installed-files")
            source_file.write_bytes(b"drift")
            with self.assertRaisesRegex(ValueError, "clock.py"):
                check_archive(run, conditions, simulator=engine, **strict_args)
        shutil.rmtree(self.site)
        shutil.rmtree(self.checkpoint)
        self.tokenizer.unlink()
        self.assertEqual(knowledge_example(conditions, None)["output"][0]["id"], "K045")
        with patch("integrity.PROFILE", aligned_profile), patch("integrity.DEPENDENCY", {"runtimeSha256": "runtime"}):
            with patch("provenance.module_file", side_effect=AssertionError("Offline check touched provider")):
                self.assertEqual(check_archive(run, conditions)["mode"], "archive")
                exported = examples(run)
                self.assertEqual(exported["model"]["input"], trace["tool_choice"]["observation"])
                self.assertEqual(exported["rules"]["output"], {"target": "route"})
            self.assertTrue(self.strict(archive, components))
            dump(run / "input/b03-demand-ends.json", {"schema": "b03.demand-ends.v1", "ends": {}})
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                check_archive(run, conditions)
            (run / "RUN_FILES.json").unlink()
            seal(run)
            with self.assertRaisesRegex(ValueError, "Activity overlay differs"):
                check_archive(run, conditions)

    def test_legacy_unavailable_knowledge_never_substitutes_current_edition(self):
        components = self.components().activate("I")
        foreign = self.components(knowledge={"source": str(ROOT / "knowledge/technical-v2.json"), "version": "1.1.0"})
        identity = {"components": components.identity, "implementationSha256": {"run.py": sha256(ROOT / "run.py")}}
        check_recorded(identity)
        old = knowledge_example(identity, None)
        self.assertEqual(old["output"][0]["version"], "1.0.0")
        unavailable = knowledge_example(identity, foreign)
        self.assertEqual(unavailable["status"], "legacy-source-unavailable")
        self.assertIsNone(unavailable["output"])
        self.assertEqual(knowledge_example({}, None)["status"], "knowledge-unrecorded")

    def test_both_native_checkpoint_formats_still_require_original_bytes(self):
        root = self.root
        spec_path = root / "config/model.json"
        files = self.checkpoint
        for name in ("tokens.json", "shard.safetensors", "training.pt", "metadata.json"):
            (files / name).write_bytes(name.encode())
        tokens = {"tokens.json": sha256(files / "tokens.json")}
        native = {"training.pt": sha256(files / "training.pt"), "metadata.json": sha256(files / "metadata.json")}
        hf = {"shard.safetensors": sha256(files / "shard.safetensors")}
        for format_name, weights in (("hf-safetensors", hf), ("native", native)):
            spec = {"format": format_name, "tokenizerSha256": tokens, "weightFilesSha256": hf,
                    "nativeVersions": {"18": {"checkpointSha256": native["training.pt"],
                                               "metadataSha256": native["metadata.json"]}}}
            dump(spec_path, spec)
            identity = {"version": 18, "modelSpecSha256": sha256(spec_path), "checkpointFilesSha256": weights}
            self.assertEqual(model_assets(root, True, identity, files, None)["checkpoint"], weights)
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                model_assets(root, True, identity, None, None)
            name = next(iter(weights))
            previous = (files / name).read_bytes()
            (files / name).write_bytes(b"drift")
            with self.assertRaisesRegex(ValueError, name):
                model_assets(root, True, identity, files, None)
            (files / name).write_bytes(previous)


if __name__ == "__main__":
    unittest.main()
