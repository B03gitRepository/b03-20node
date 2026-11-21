"""Independent rule search, transactional context and archived RC knowledge receipts."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/rules"), str(ROOT / "src/rules/vendor/src")]
import strong_tools
from component_config import load_components
from rule_component import CheckedRules, bind_proposal, external_rules
from tech_knowledge import check_opening_advice, opening_lookup, opening_outcome, query, verify_work_order_queries
from test_rule_component import make_bound, rebind, select_i


class ExternalRule(unittest.TestCase):
    def setUp(self):
        self.components = load_components(ROOT / "config/branch-rules.json").activate("M")
        self.rules = external_rules(SimpleNamespace(components=self.components, context={}))
        self.bound = make_bound()

    def test_distinct_solver_and_unchanged_legacy_actions(self):
        native = strong_tools.enumerate_actions(self.bound["snapshot"], self.bound["authority"], self.bound["config"])
        offered = self.rules.catalog(self.bound, {})
        self.assertEqual(native, [a for a in offered if a["id"] != "M"])
        action = next(a for a in offered if a["id"] == "M")
        template = next(a for a in native if a["id"] == "L")
        self.assertEqual(action["scope"], template["scope"])
        self.assertEqual(action["budget"], template["budget"])
        self.assertNotIn("M", self.components.model["symbols"])
        with patch.object(strong_tools, "solve_action", side_effect=AssertionError("native solver called")):
            target, audit, checked = select_i(self.rules, self.bound, "M")
        self.assertTrue(checked["valid"])
        self.assertEqual(target["one"]["linkIds"], ["wide"])
        self.assertEqual(audit["allocator"], "widest_residual")
        with patch.object(self.rules.implementation, "solve_action", side_effect=AssertionError("external solver called")):
            native_target, _, checked = select_i(self.rules, self.bound, "I")
        self.assertTrue(checked["valid"])
        self.assertEqual(native_target["one"]["linkIds"], ["fast"])
        self.bound["authority"]["allowedMethods"].remove("pool_greedy")
        rebind(self.bound)
        legal = self.rules.catalog(self.bound, {})
        self.assertNotIn("M", [a["id"] for a in legal])
        self.assertIn("I", [a["id"] for a in legal])

    def test_controller_registration_and_action_remain_fixed(self):
        spec = deepcopy(self.components.rules)
        spec["options"]["customTools"]["M"]["budget"] = "unbounded"
        with self.assertRaisesRegex(ValueError, "fixed search budget"):
            CheckedRules(self.rules.implementation, spec, self.components.symbols)
        supplied = self.rules.implementation.enumerate_actions(self.bound["snapshot"], self.bound["authority"], {})
        for field, v in (("scope", {"kind": "all", "serviceIds": ["one", "two"]}), ("budget", "other"), ("id", "I")):
            fake = [dict(supplied[0], **{field: v})]
            with self.subTest(field=field), patch.object(self.rules.implementation, "enumerate_actions", return_value=fake):
                with self.assertRaisesRegex(ValueError, "authorized"):
                    self.rules.catalog(self.bound, {})

    def test_rejection_and_exception_discard_nested_context(self):
        self.rules.context = {"seen": ["old"]}
        tool = self.rules.implementation.solve_action

        def changing(action, bound, context=None):
            context["seen"].append("pending")
            result = tool(action, bound, context)
            if scenario == "throws":
                raise RuntimeError("provider stopped")
            if scenario != "valid":
                result[1]["pathSearch"] = scenario
            return result

        with patch.object(self.rules.implementation, "solve_action", side_effect=changing):
            for scenario in ("throws", [], {"one": 7}, {"one": {"expanded": "7", "selectedCandidates": 1}},
                             {"one": {"expanded": 7, "selectedCandidates": True}}, {"one": {"expanded": 8001, "selectedCandidates": 1}}):
                with self.subTest(scenario=scenario), self.assertRaises((RuntimeError, ValueError)):
                    select_i(self.rules, self.bound, "M")
                self.assertEqual(self.rules.context, {"seen": ["old"]})
            scenario = "valid"
            select_i(self.rules, self.bound, "M")
        self.assertEqual(self.rules.context, {"seen": ["old", "pending"]})


class OpeningKnowledge(unittest.TestCase):
    def setUp(self):
        self.components = load_components(ROOT / "config/example-knowledge.json").activate("I")
        self.state = {"now_ns": 8, "services": {"one": {"fresh": True, "active": True, "registered": True,
                      "observed_at_ns": 4, "received_at_ns": 6, "revision": 2}}}
        self.diagnosis = {"authority": {"executableServiceIds": ["one"]},
                          "services": {"one": {"authorized": True, "status": "observed_pending_admission"}}}
        self.admission = {"mode": "residual", "search": [{"id": "one"}], "missingAuthorized": ["one"],
                          "applied": ["one"], "heldBySelectedAction": False, "feasibleAdditions": ["one"]}
        self.choices = {"one": {"accepted": True, "required_wire_bps": 1036}}

    def lookup(self, components=None, mode="residual"):
        return opening_lookup(components or self.components, "W1", "state", self.state, self.diagnosis, mode, "I")

    def test_real_provider_is_called_only_at_applicable_opening(self):
        factory = self.components.factory("knowledge")
        with patch.object(self.components, "factory", wraps=self.components.factory) as build:
            self.assertIsNone(self.lookup(mode="original"))
            self.assertEqual(build.call_count, 0)
            receipt = self.lookup()
            build.assert_called_once_with("knowledge")
        self.assertEqual(receipt["output"], query(self.components, identifier="K044"))
        self.assertEqual(receipt["component"]["version"], "1.1.0")
        self.assertEqual(receipt["input"]["pendingAuthorized"]["one"]["received_at_ns"], 6)
        self.assertTrue(opening_outcome(receipt, self.admission, self.choices, ["one"])["services"]["one"]["addedByOpening"])
        bad = Mock(lookup=Mock(return_value=["K045"]))
        with patch.object(self.components, "factory", return_value=lambda **kw: bad):
            with self.assertRaisesRegex(ValueError, "pinned edition"):
                self.lookup()
        self.diagnosis["authority"]["executableServiceIds"] = []
        with patch.object(self.components, "factory", side_effect=AssertionError("should not query")):
            self.assertIsNone(self.lookup())
        self.assertTrue(callable(factory))

    def test_archive_checks_trigger_source_actual_result_and_disposition_without_import(self):
        receipt = self.lookup()
        before = {"one": None}
        after = {"one": {"nodes": ["a", "b"], "linkIds": ["fast"], "allocatedBps": 1036}}
        snapshot = {"requests": {"one": {"requirement": {"bandwidthBps": 1036}}}}
        receipt["procedureCheck"] = check_opening_advice(receipt, before, after, snapshot,
                                                       self.diagnosis["authority"], self.admission)
        with self.assertRaisesRegex(ValueError, "prior path"):
            check_opening_advice(receipt, {"one": {"allocatedBps": 1}}, after, snapshot,
                                 self.diagnosis["authority"], self.admission)
        receipt["disposition"] = opening_outcome(receipt, self.admission, self.choices, ["one"])
        common = {"work_id": "W1", "time_ns": 8, "snapshot_id": "state"}
        events = [{"event": "b03_om_diagnosis", **common, **self.diagnosis},
                  {"event": "b03_observation_evidence", **common, "purpose": "plan", "state": self.state},
                  {"event": "b03_rc_plan", **common, "authorized": ["one"], "real_solver_calls": 1,
                   "tool_choice": {"selected": "I"}, "decisions": self.choices, "install_ids": ["one"],
                   "solver_audit": {"openingAdmission": self.admission}, "knowledge_io": receipt}]
        reg = self.components.identity["knowledge"]
        cond = {"mode": "workorder", "rcPolicy": {"initialAdmission": "residual"}, "components": self.components.identity,
                "provenance": {"activeComponents": {"knowledge": reg}, "knowledge": {"id": reg["id"],
                              "version": reg["version"], "text": self.components.knowledge_path.read_bytes().decode()}}}
        with patch.object(self.components, "factory", side_effect=AssertionError("provider imported")):
            self.assertEqual(verify_work_order_queries(cond, events)[0]["pending"], ["one"])
        for key in ("input", "component", "output", "procedureCheck", "disposition", "remove"):
            wrong = deepcopy(events)
            if key == "remove":
                del wrong[-1]["knowledge_io"]
            else:
                wrong[-1]["knowledge_io"][key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_work_order_queries(cond, wrong)


if __name__ == "__main__":
    unittest.main()
