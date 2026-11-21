"""Configuration and read-only knowledge regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from component_config import load_components
from tech_knowledge import query


class KnowledgeContract(unittest.TestCase):
    def test_switch_edition_without_touching_work_order_components(self):
        default = load_components()
        newer = load_components(ROOT / "config/knowledge-v2.json")
        for kind in ("catalog", "model", "rules"):
            self.assertEqual(getattr(default, kind), getattr(newer, kind))
        old = query(default, identifier="K045")
        new = query(newer, topic="clock")
        self.assertEqual((old[0]["id"], new[0]["id"]), ("K045", "K045"))
        self.assertEqual((old[0]["version"], new[0]["version"]), ("1.0.0", "1.1.0"))
        self.assertNotEqual(old[0]["content"], new[0]["content"])
        self.assertTrue(all((ROOT / p).is_file() for p in new[0]["sources"]))
        self.assertEqual(query(default, identifier="unknown"), [])

    def test_lookup_is_independent_of_provider_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"extends":str(ROOT / "config/components.json"),
                            "model":{"entrypoint":"not_installed_b03_model:build"}}), encoding="utf-8")
            self.assertEqual(query(load_components(path), topic="admission")[0]["id"], "K044")

    def test_a_different_mapping_requires_a_catalog_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"extends":str(ROOT / "config/components.json"),
                            "catalog":{"symbols":["A","C","B"]}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "immutable"):
                load_components(path)
