import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from component_config import load_components
from scene import validate_engine_lock, verify_engine
from simulator_component import LOCK_MEMBER, archived_engine_lock, runtime_identity, select_engine


def sha(content):
    return hashlib.sha256(content).hexdigest()


def runtime(files):
    return sha(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())


class SimulatorRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        contents = {"src/resnet/__init__.py": b"", "src/resnet/simulation/simulation.py": b"class Simulation: pass\n",
                    "configs/common.json": b"{}", "pyproject.toml": b'[project]\nname="partner"\nversion="2.0"\n'}
        files = {}
        for name, content in contents.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
            files[name] = sha(content)
        self.lock = {"distribution": "partner", "version": "2.0", "files": files, "runtimeSha256": runtime(files)}

    def test_active_engine_inventory_and_metadata(self):
        self.assertEqual(verify_engine(self.root, self.lock), self.lock["runtimeSha256"])
        with self.assertRaisesRegex(ValueError, "metadata"):
            verify_engine(self.root, dict(self.lock, version="3.0"))
        helper = self.root / "src/resnet/helper.py"
        helper.write_text("def help(): pass\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "helper.py"):
            verify_engine(self.root, self.lock)
        helper.unlink()
        (self.root / "configs/common.json").write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configs/common.json"):
            verify_engine(self.root, self.lock)

    def test_lock_cannot_relabel_an_incomplete_or_unsafe_table(self):
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            validate_engine_lock(dict(self.lock, runtimeSha256="0"*64))
        for name in ("../foreign", "/etc/passwd", "dir\\foreign", "."):
            with self.subTest(name=name):
                files = dict(self.lock["files"])
                files[name] = "0"*64
                with self.assertRaisesRegex(ValueError, "lock member"):
                    validate_engine_lock(dict(self.lock, files=files, runtimeSha256=runtime(files)))
        files = dict(self.lock["files"])
        del files["pyproject.toml"]
        with self.assertRaisesRegex(ValueError, "pyproject"):
            validate_engine_lock(dict(self.lock, files=files, runtimeSha256=runtime(files)))

    def test_archive_binds_registry_asset_and_provider_without_imports(self):
        raw = json.dumps(self.lock).encode()
        (self.root / "runtime.json").write_bytes(raw)
        cfg = {"extends": str(ROOT / "config/example-simulator.json"),
               "simulator": {"provenance": {"assets": {"engineLock": "runtime.json"}}}}
        manifest = self.root / "partner.json"
        manifest.write_text(json.dumps(cfg), encoding="utf-8")
        components = load_components(manifest).activate("I")
        blob, lock = select_engine(components)
        self.assertEqual(blob, raw)
        registration = components.identity["simulator"]
        identity = runtime_identity(registration, lock, blob)
        record = {"components": {"simulator": registration}, "simulatorRuntime": identity,
                  "releaseManifestSha256": lock["runtimeSha256"], "provenance": {"providers": {"simulator":
                    {"assets": {"engineLock": {"type": "file", "sha256": sha(raw)}}}}}}
        (self.root / LOCK_MEMBER).write_bytes(raw)
        self.assertEqual(archived_engine_lock(self.root, record, {LOCK_MEMBER}), self.lock)
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            archived_engine_lock(self.root, {k: v for k, v in record.items() if k != "simulatorRuntime"}, set())
        record["provenance"]["providers"]["simulator"]["assets"]["engineLock"]["sha256"] = "0"*64
        with self.assertRaisesRegex(ValueError, "registered provider asset"):
            archived_engine_lock(self.root, record, {LOCK_MEMBER})

    def test_historical_release_uses_its_accepted_lock(self):
        legacy = {"releaseManifestSha256": "original"}
        lock = {"runtimeSha256": "original"}
        self.assertIs(archived_engine_lock(self.root, legacy, {}, default_lock=lock), lock)
        with self.assertRaisesRegex(ValueError, "historical"):
            archived_engine_lock(self.root, legacy, {}, default_lock={"runtimeSha256": "other"})


if __name__ == "__main__":
    unittest.main()
