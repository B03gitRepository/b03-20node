"""Packet model boundary tests using synthetic observations and CPU-only adapter fixtures."""
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from component_config import load_components
from frozen_policy import DecisionPolicy, FrozenActor, SCHEMA
from model_component import validate_model_choice


class Registration:
    def __init__(self, factory, mode="frozen"):
        self.symbols = ["A", "B", "C", "D"]
        self.model = dict(id="unit.adapter", version="1.0", entrypoint="unit:build_model",
                          catalogVersion="unit.catalog", symbols=["A", "B"], mode=mode)
        self.build = factory

    def factory(self, kind):
        if kind != "model":
            raise ValueError(kind)
        return self.build


class Adapter:
    def __init__(self, answer):
        self.answer = answer

    def choose(self, observation):
        return self.answer(observation)


def registered(actor):
    return DecisionPolicy("model", components=Registration(lambda **kwargs: actor))


def choose(policy, symbols=("D", "B", "A")):
    actions = [{"id": s, "tool": "unit-" + s, "scope": {"kind": "authorized", "serviceIds": []},
                "pool": "none", "budget": "small", "stage": "repair"} for s in symbols]
    config = dict(path_limit=2, path_expansion_budget=10, search_budget=10, search_service_limit=2)
    return policy.select(actions, {"now_ns": 3, "links": {}, "services": {}, "confirmed": {}},
                         {"nodes": {"unit-node": {"_online": True}}, "links": {}},
                         {"reported_wire_bps": {}}, {"services": {}}, config)


class SampledFixture:
    """Exercise sampling and checkpoint hooks without training a production model."""
    def __init__(self, **kwargs):
        self.version = 7
        self.rng = random.Random(9)

    def choose(self, obs):
        first, second = obs["legalActions"]
        behavior = {first: 0.25, second: 0.75}
        selected = first if self.rng.random() < behavior[first] else second
        return selected, {"policyVersion": self.version, "behaviorProbabilities": behavior,
                          "behaviorProbability": behavior[selected], "oldLogProbability": math.log(behavior[selected])}

    def save_checkpoint(self, path):
        Path(path).write_text(json.dumps([self.version, self.rng.getstate()]), encoding="utf-8")

    def restore_checkpoint(self, path):
        self.version, state = json.loads(Path(path).read_text(encoding="utf-8"))
        self.rng.setstate((state[0], tuple(state[1]), state[2]))


class ModelContracts(unittest.TestCase):
    def test_version_only_factory_and_delivered_mask(self):
        builds = []
        inputs = []

        def build_model(*, checkpoint, tokenizer, device, config):
            builds.append((checkpoint, tokenizer, device, deepcopy(config)))
            config["symbols"].clear()

            def pick(obs):
                inputs.append(obs)
                return obs["legalActions"][-1], {"policyVersion": "policy-3"}

            return Adapter(pick)

        registration = Registration(build_model)
        policy = DecisionPolicy("model", "ck", "tok", "cpu", "audit", registration)
        policy.record_component_io = True
        tool, receipt = choose(policy)
        self.assertEqual(builds, [("ck", "tok", "cpu", registration.model)])
        self.assertEqual((tool["id"], receipt["selected"], receipt["policyVersion"]), ("B", "B", "policy-3"))
        self.assertEqual(receipt["observation"]["legalActions"], ["A", "B"])
        self.assertEqual(receipt["observation"]["actionCatalog"]["A"]["tool"], "unit-A")
        self.assertEqual(receipt["observation"], inputs[0])
        inputs[0]["actionCatalog"].clear()
        self.assertTrue(validate_model_choice(receipt, policy.identity))
        self.assertEqual(receipt["adapterId"], policy.identity["adapterId"])
        self.assertEqual(policy.identity["adapterVersion"], "1.0")
        self.assertTrue({"parameterVersion", "legalProbabilities", "behaviorProbability", "inputTokens",
                         "inputSha256"}.isdisjoint(receipt))
        self.assertGreater(receipt["selectionWallNs"], 0)

    def test_model_cannot_extend_mask_or_replace_host_fields(self):
        def alter_input(obs):
            obs["legalActions"].append("D")
            return "D", {"policyVersion": 3}

        bad = [alter_input, lambda obs: ("A", {"policyVersion": 3, "observation": {"legalActions": ["A"]}})]
        for key in ("method", "modelCalled", "selected", "selectionWallNs", "adapterId", "catalogVersion"):
            bad.append(lambda obs, key=key: ("A", {"policyVersion": 3, key: "forged"}))
        for answer in bad:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                choose(registered(Adapter(answer)))
        policy = registered(Adapter(lambda obs: ("A", {"policyVersion": 3})))
        for bad_catalog in (("C",), ("A", "A"), ("A", "X")):
            with self.subTest(catalog=bad_catalog), self.assertRaises(ValueError):
                choose(policy, bad_catalog)

    def test_incomplete_mapping_invalid_diagnostics_and_version_changes(self):
        actor = Adapter(lambda obs: ("A", {"policyVersion": 1}))
        for symbols in ([], ["A", "C", "X"], ["A", "A"]):
            cfg = Registration(lambda **kwargs: actor)
            cfg.model["symbols"] = symbols
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                DecisionPolicy("model", components=cfg)
        reverse = Registration(lambda **kwargs: actor)
        reverse.model["symbols"] = ["B", "A"]
        _, receipt = choose(DecisionPolicy("model", components=reverse))
        self.assertEqual(receipt["observation"]["legalActions"], ["B", "A"])
        actor.symbols = ["B", "A"]
        with self.assertRaises(ValueError):
            registered(actor)
        for details in ({}, {"policyVersion": " "}, {"policyVersion": False},
                        {"policyVersion": 1, "parameterVersion": 2}, {"policyVersion": 1, "inputTokens": True},
                        {"policyVersion": 1, "inputSha256": "abc"}, {"policyVersion": 1, "metric": float("nan")}):
            with self.subTest(details=details), self.assertRaises(ValueError):
                choose(registered(Adapter(lambda obs: ("A", details))))
        iterator = iter(["p1", "p2"])
        policy = registered(Adapter(lambda obs: ("A", {"policyVersion": next(iterator)})))
        choose(policy)
        with self.assertRaises(ValueError):
            choose(policy)

    def test_offline_receipt_rejects_false_identity_evidence_and_probabilities(self):
        policy = registered(Adapter(lambda obs: ("B", {"policyVersion": 3})))
        _, sample = choose(policy)
        bad = [
            {"adapterId": "other"}, {"catalogVersion": "other"}, {"selected": "D"}, {"action": "A"},
            {"modelCalled": False}, {"method": "I"}, {"selectionWallNs": -1},
            {"legalProbabilities": {"A": 1}}, {"legalProbabilities": {"A": -0.1, "B": 1.1}},
            {"legalProbabilities": {"A": float("nan"), "B": 0.1}}, {"behaviorProbability": 0},
            {"behaviorProbabilities": {"A": 0.5, "B": 0.5}, "behaviorProbability": 0.4},
            {"behaviorProbabilities": {"A": 0.5, "B": 0.5}, "behaviorProbability": 0.5, "oldLogProbability": 0.0}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_model_choice(dict(sample, **change), policy.identity)
        for change in ("missing", "scope", "index"):
            altered = deepcopy(sample)
            cat = altered["observation"]["actionCatalog"]
            if change == "missing":
                del cat["B"]
            elif change == "scope":
                del cat["B"]["scope"]
            else:
                cat["A"]["serviceSet"] = 999
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_model_choice(altered, policy.identity)

    def test_reference_and_builtin_delegate_to_the_same_frozen_actor(self):
        builtin = load_components()
        example = load_components(ROOT / "config/example-model.json")
        symbols = builtin.model["symbols"]
        inits, forwards = [], []

        def init(actor, checkpoint, tokenizer, device):
            inits.append((checkpoint, tokenizer, device))
            actor.symbols = list(symbols)
            actor.version = 17
            actor.identity = {"kind": "frozen-model", "version": 17, "symbols": list(symbols), "schema": SCHEMA,
                              "selection": "greedy_legal_token", "checkpointFilesSha256": {"fixture": "0" * 64}}

        def forward(actor, obs):
            forwards.append(deepcopy(obs))
            legal = obs["legalActions"]
            probs = {s: float(s == "B") for s in legal}
            return "B", {"observation": obs, "parameterVersion": 17, "inputTokens": 11,
                         "inputSha256": "a" * 64, "legalProbabilities": probs, "behaviorProbability": 1.0}

        with patch.object(FrozenActor, "__init__", init), patch.object(FrozenActor, "choose", forward):
            legacy = DecisionPolicy("model", "ck", "tok", "cpu", "audit")
            default = DecisionPolicy("model", "ck", "tok", "cpu", "audit", builtin)
            alternative = DecisionPolicy("model", "ck", "tok", "cpu", "audit", example)
            _, old = choose(legacy)
            _, first = choose(default)
            _, second = choose(alternative)
        self.assertEqual(inits, [("ck", "tok", "cpu")] * 3)
        self.assertEqual(forwards, [forwards[0]] * 3)
        self.assertNotEqual(first["adapterId"], second["adapterId"])
        self.assertEqual(default.identity["checkpointFilesSha256"], alternative.identity["checkpointFilesSha256"])
        for key in ("selected", "policyVersion", "inputTokens", "inputSha256", "legalProbabilities", "behaviorProbability"):
            self.assertEqual(first[key], second[key])
        del old["policyVersion"]
        self.assertTrue(validate_model_choice(old, legacy.identity))
        for alteration in ({"parameterVersion": 19}, {"policyVersion": "17"}, {"selected": "A"},
                           {"behaviorProbability": 0.9}, {"inputTokens": 0}):
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                validate_model_choice(dict(second, **alteration), alternative.identity)
        for field in ("legalProbabilities", "parameterVersion", "inputTokens", "inputSha256", "behaviorProbability"):
            incomplete = dict(second)
            del incomplete[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_model_choice(incomplete, alternative.identity)

    def test_online_sampling_contract_and_checkpoint_resume(self):
        no_checkpoint = Registration(lambda **kw: Adapter(lambda obs: ("A", {"policyVersion": 1})), "online-ppo")
        with self.assertRaises(ValueError):
            DecisionPolicy("model", components=no_checkpoint)
        cfg = Registration(SampledFixture, "online-ppo")
        policy = DecisionPolicy("model", components=cfg)
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "sampler.json"
            choose(policy)
            policy.actor.version = 8
            policy.actor.save_checkpoint(checkpoint)
            expected = [choose(policy)[1] for _ in range(8)]
            restored = DecisionPolicy("model", checkpoint=checkpoint, components=cfg)
            replay = [choose(restored)[1] for _ in range(8)]
        self.assertEqual(restored.identity["initialPolicyVersion"], 8)
        keys = ("selected", "policyVersion", "behaviorProbabilities", "behaviorProbability", "oldLogProbability")
        self.assertEqual([[row[k] for k in keys] for row in replay], [[row[k] for k in keys] for row in expected])
        self.assertEqual({row["selected"] for row in replay}, {"A", "B"})
        for bad in ({"behaviorProbability": 0.123}, {"policyMode": "greedy"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_model_choice(dict(replay[0], **bad), restored.identity)
        incomplete = dict(replay[0])
        del incomplete["behaviorProbabilities"]
        with self.assertRaises(ValueError):
            validate_model_choice(incomplete, restored.identity)

    def test_fixed_rules_and_five_argument_constructor(self):
        fixed = DecisionPolicy("B", None, None, "cpu", "audit")
        _, choice = choose(fixed)
        self.assertEqual((choice["method"], choice["selected"], choice["modelCalled"]), ("B", "B", False))
        self.assertEqual(fixed.identity, {"kind": "rule", "method": "B", "initialAdmission": "audit"})
        combined = DecisionPolicy("combined", components=Registration(None))
        self.assertEqual(choose(combined)[0]["id"], "D")


if __name__ == "__main__":
    unittest.main()
