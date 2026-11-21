"""Regression checks for complete model inputs, exercised INT8 operators and measured U returns."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from frozen_policy import SCHEMA, canonical
from model_component import validate_model_choice
from packet_input import InputTooLong, decode_observation, encode_observation, fit_complete_input
from packet_learner import unweighted_advantage
from packet_training import main as training_main, samples_from_work_orders, training_workspace


def observation():
    entry = dict(tool="t", scope="repair", serviceSet=0, pool="legal", budget="small", stage="repair")
    return {"schema": SCHEMA, "timeNs": 2, "nodes": {"columns": ["id", "up"], "defaults": [True],
            "rows": [[i, 0] for i in range(11)] + [[15, 1, False]]},
            "authorizedServiceSets": [[1, 2, 3, 6, 7, 8], [5, 3, 3, 4]],
            "legalActions": ["A", "B"], "actionCatalog": {"A": entry, "B": entry}, "links": {"rows": []}}


def frozen_choice(obs):
    return {"method": "model", "selected": "A", "modelCalled": True, "observation": obs,
            "inputTokens": 4, "inputSha256": "a"*64, "legalProbabilities": {"A": 0.7, "B": 0.3},
            "parameterVersion": 18, "behaviorProbability": 1.0, "selectionWallNs": 260}


class ModelRuntime(unittest.TestCase):
    def test_over_limit_reversible_input_and_archive_validation(self):
        obs = observation()
        original = deepcopy(obs)
        self.assertEqual(decode_observation(encode_observation(obs)), original)
        self.assertEqual(obs, original)
        actor = SimpleNamespace(limit=4, tokenizer=SimpleNamespace(
            apply_chat_template=lambda messages, **kw: [1, 2, 3]))
        messages = [{"role": "system", "content": "choose"}, {"role": "user", "content": canonical(obs)}]
        ids, delivered, proof = fit_complete_input(actor, obs, list(range(8)), messages)
        self.assertEqual(ids, [1, 2, 3])
        self.assertEqual(decode_observation(json.loads(delivered[-1]["content"])), original)
        identity = {"kind": "frozen-model", "version": 18, "symbols": ["A", "B"], "schema": SCHEMA}
        ch = dict(frozen_choice(obs), **proof)
        ch.update(inputTokens=len(ids), inputSha256=proof["actualInputSha256"])
        self.assertTrue(validate_model_choice(ch, identity))
        for change in ({"losslessVerified": False}, {"tokenLimit": 2}, {"rawObservationSha256": "a"*64},
                       {"actualInputSha256": "b"*64}, {"deliveredTokens": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_model_choice(dict(ch, **change), identity)
        actor.limit = 2
        with self.assertRaises(InputTooLong) as error:
            fit_complete_input(actor, obs, list(range(8)), messages)
        self.assertEqual((error.exception.receipt["originalTokens"], error.exception.receipt["deliveredTokens"]), (8, 3))

    def test_int8_requires_real_full_execution_and_positive_measured_time(self):
        ch = frozen_choice(observation())
        identity = {"kind": "frozen-model", "version": 18, "symbols": ["A", "B"],
                    "precision": "dynamic-int8-linear", "quantization":
                    {"quantizedLinearModules": 5, "linearModules": 5, "quantizedDtype": "qint8"}}
        ch.update(inferenceWallNs=220, executedInt8LinearModules=5)
        self.assertTrue(validate_model_choice(ch, identity))
        for changes in ({"inferenceWallNs": 261}, {"inferenceWallNs": 0}, {"executedInt8LinearModules": 4}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_model_choice(dict(ch, **changes), identity)

    def test_ppo_receives_unweighted_future_credit_and_only_authorized_training_scripts(self):
        def put(path, obj):
            path.write_text(json.dumps(obj), encoding="utf8")

        def trace(path, rows):
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf8")

        obs = observation()
        condition = dict.fromkeys(("sourceHashes", "releaseManifestSha256", "originalConfigSha256",
            "simulation", "services", "officialDefinitions", "authority", "dropInstallAckAtDestination"))
        condition.update(nodeCount=100, script="04", mode="workorder")
        report = {"actualModelCalls": 1, "business": {"U": 1e-8, "W": 1e10}}
        with tempfile.TemporaryDirectory() as dirname:
            sampled, fixed = (Path(dirname)/name for name in ("sampled", "fixed"))
            for folder in (sampled, fixed):
                folder.mkdir()
            put(sampled/"CONDITIONS.json", dict(condition, rcPolicy={"mode": "online-ppo"}))
            put(fixed/"CONDITIONS.json", dict(condition, rcPolicy={"method": "I"}))
            trace(sampled/"b03.jsonl", [{"event": "b03_rc_plan", "tool_choice":
                {"modelCalled": True, "observation": obs, "selected": "A"}}])
            for folder, count in ((sampled, 1), (fixed, 2)):
                trace(folder/"business-intervals.jsonl", [{"start_ns": 0, "end_ns": 10,
                    "unmet": list(range(count)), "U_service_ns": 10*count, "W_service_ns": 999999}])
            samples = samples_from_work_orders(sampled, fixed, report)
            _, credit = samples[0]
            self.assertEqual(credit["rawReturnSeconds"], -8e-9)
            self.assertEqual(credit["fixedIReturnSeconds"], -16e-9)
            self.assertEqual(unweighted_advantage(credit), 8e-9)
            for bad in ({"script": "01"}, {"nodes": 20}, {"rawReturnSeconds": -7e-9}, {"toNs": 1}):
                with self.subTest(change=bad), self.assertRaises(ValueError):
                    unweighted_advantage(dict(credit, **bad))
            put(sampled/"CONDITIONS.json", dict(condition, script="01", rcPolicy={"mode": "online-ppo"}))
            with self.assertRaises(ValueError):
                samples_from_work_orders(sampled, fixed, report)

    def test_training_preflight_protects_assets_without_creating_locks_or_workspaces(self):
        import packet_training
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            engine = root / "bundle/resnet_py"
            (engine / "src/resnet").mkdir(parents=True)
            data = root / "official"
            data.mkdir()
            ckpt = root / "weights"
            ckpt.mkdir()
            tok = root / "tokenizer"
            tok.mkdir()
            archive = root / "official.tar"
            archive.write_bytes(b"preserve")
            provider = root / "registered-provider-assets"
            provider.mkdir()
            manifest = root / "components.json"
            manifest.write_text(json.dumps({"extends":str(ROOT/"config/packet-ppo.json"),
                "model":{"provenance":{"assets":{"extra":str(provider)}}}}),encoding="utf8")
            kwargs = dict(command="step", round=1, simulator=engine.parent, data=data, checkpoint=ckpt,
                          tokenizer=tok, components=manifest)
            for source, dest in ((data, data/"new"), (data, engine/"new"), (data, ckpt/"new"),
                                 (data, tok/"new"), (data, provider/"new"), (archive, archive)):
                argv = ["train.py","step","--workspace",str(dest),"--round","1","--script","04",
                        "--checkpoint",str(ckpt),"--tokenizer",str(tok),"--simulator",str(engine.parent),
                        "--data",str(source),"--components",str(manifest),"--services","example",
                        "--start","0","--end","4","--drain","0"]
                with self.subTest(dest=dest), patch.object(sys, "argv", argv), self.assertRaisesRegex(ValueError,"read-only"):
                    training_main()
                if dest != archive:
                    self.assertFalse(dest.exists())
            self.assertEqual(archive.read_bytes(), b"preserve")
            official = root / "bundle/data/scenarios" / packet_training.PROFILE["officialScenario"]
            official.mkdir(parents=True)
            (official / "net-topo.json").write_bytes(b"original")
            sibling = engine.parent / "outputs"
            args = SimpleNamespace(**dict(kwargs, data=engine.parent), workspace=sibling)
            self.assertEqual(training_workspace(args), sibling)
            self.assertEqual(packet_training.data_locations(args.data)[0], official)
            self.assertEqual(args.data, engine.parent)
            self.assertEqual(args.simulator, engine.parent)
            self.assertFalse(sibling.exists())
            # An outer directory may hold both inputs and outputs. A named output must still avoid them.
            self.assertEqual(training_workspace(SimpleNamespace(**kwargs, workspace=root)), root)
            collision = root/"attempts"
            collision.mkdir()
            with self.assertRaisesRegex(ValueError,"read-only"):
                training_workspace(SimpleNamespace(**dict(kwargs, checkpoint=collision), workspace=root))
            self.assertFalse((root/"session.lock").exists())

    def test_component_assets_are_read_only_for_step_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            engine, work = root / "resnet_py", root / "work"
            (engine / "src/resnet").mkdir(parents=True)
            work.mkdir()
            for name in ("weights", "tokenizer", "official", "provider"):
                (root / name).mkdir()
            parent, manifest = root / "provider.json", root / "components.json"
            knowledge = root / "edition.json"
            knowledge.write_text("{}", encoding="utf8")
            attachment = root / "provider/table.txt"
            attachment.write_bytes(b"reference")
            parent.write_text(json.dumps({"extends": str(ROOT / "config/packet-ppo.json"),
                "knowledge": {"source": knowledge.name, "provenance": {"assets": {
                    "lookup": "provider", "table": "provider/table.txt"}}}}), encoding="utf8")
            manifest.write_text(json.dumps({"extends": parent.name}), encoding="utf8")
            shared = ["--round", "1", "--checkpoint", str(root / "weights"), "--tokenizer", str(root / "tokenizer")]
            args = ["--simulator", str(engine), "--data", str(root / "official"), "--components", str(manifest),
                    "--script", "04", "--services", "example", "--start", "0", "--end", "4", "--drain", "0"]
            receipt_path = work / "round-001.json"
            receipt = {"snapshot": "round-001-replay.json", "options": {"components": str(manifest)}}
            receipt_path.write_text(json.dumps(receipt), encoding="utf8")
            fresh = root / "provider/output/result.json"
            for dest in (fresh, attachment, parent, knowledge):
                for route in (["step", "--workspace", str(dest), *args],
                              ["restore", "--workspace", str(work), "--report", str(dest)]):
                    with self.subTest(route=route[0], dest=dest), patch.object(
                            sys, "argv", ["train.py", *route, *shared]), self.assertRaisesRegex(ValueError, "read-only"):
                        training_main()
            # A legacy receipt can still resolve sibling assets if the knowledge source has moved.
            knowledge.rename(root / "moved-edition.json")
            restore = ["train.py", "restore", "--workspace", str(work), "--report", str(fresh), *shared]
            with patch.object(sys, "argv", restore), self.assertRaisesRegex(ValueError, "read-only"):
                training_main()
            # New receipts retain the input set even if the original manifest is gone.
            receipt["componentInputPaths"] = [str(attachment.parent)]
            receipt_path.write_text(json.dumps(receipt), encoding="utf8")
            manifest.rename(root / "moved-registration.json")
            with patch.object(sys, "argv", restore), self.assertRaisesRegex(ValueError, "read-only"):
                training_main()
            self.assertEqual(attachment.read_bytes(), b"reference")
            self.assertFalse(fresh.parent.exists())
            self.assertFalse(list(work.glob("*.lock")))

    def test_restore_report_cannot_write_to_a_registered_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work, weights, tokenizer, original = (root/n for n in ("work","weights","tokenizer","official"))
            for directory in (work,weights,tokenizer,original):
                directory.mkdir()
            receipt = {"snapshot":"round-001-replay.json", "options":{"data":str(original)}}
            (work/"round-001.json").write_text(json.dumps(receipt),encoding="utf8")
            report = original/"output"/"fresh.json"
            argv = ["train.py","restore","--round","1","--workspace",str(work),"--checkpoint",str(weights),
                    "--tokenizer",str(tokenizer),"--report",str(report)]
            with patch.object(sys,"argv",argv), self.assertRaisesRegex(ValueError,"read-only"):
                training_main()
            self.assertFalse(report.parent.exists())
            self.assertFalse((work/"round-001-replay.json.lock").exists())
            # An archived caller could have supplied the containing packet bundle as its data argument.
            official = root / "data/scenarios" / __import__("packet_training").PROFILE["officialScenario"]
            official.mkdir(parents=True)
            (official/"net-topo.json").write_bytes(b"original")
            receipt["options"]["data"] = str(root)
            (work/"round-001.json").write_text(json.dumps(receipt),encoding="utf8")
            args = SimpleNamespace(command="restore", round=1, workspace=work, checkpoint=weights,
                                   tokenizer=tokenizer, report=work/"report.json")
            self.assertEqual(training_workspace(args), work)
