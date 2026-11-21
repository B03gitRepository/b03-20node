"""Controller-boundary and algorithm regressions using the original QoS and capacity validator."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/rules"), str(ROOT / "src/rules/vendor/src")]
import strong_tools
from component_config import load_components
from example_rules import build_rules
from rule_component import CheckedRules, begin_sample, bind_proposal, external_rules

SYMBOLS = tuple("ABCDEFGHIJKL")
SPEC = {"id": "example.residual-first", "version": "1.0.0", "entrypoint": "example_rules:build_rules",
        "catalogVersion": "b03.tools.v1"}


def make_bound():
    def link(name, capacity, delay):
        return {"linkId": name, "srcNodeId": "a", "dstNodeId": "b", "_online": True,
                "params": {"bandwidthBps": capacity, "delayMs": delay, "jitterMs": 0, "ber": 0, "packetLossRate": 0}}

    demand = {"serviceId": "one", "srcNodeId": "a", "dstNodeId": "b", "priority": 3,
              "requirement": {"bandwidthBps": 1000, "delayMs": 10, "jitterMs": 5, "ber": 0.01, "packetLossRate": 0.01}}
    links = {r["linkId"]: r for r in (link("fast", 1100, 1), link("wide", 4000, 3), link("late", 8000, 20))}
    s = {"nodes": {"a": {"_online": True}, "b": {"_online": True}}, "links": links, "requests": {"one": demand},
         "assignments": {"one": None}, "observedAtSec": 5, "stateVersion": "fresh", "configVersion": "v1",
         "objectiveVersion": "priority-classes-then-min-changes-v1"}
    config = {"path_limit": 4, "path_expansion_budget": 100, "search_budget": 100, "search_service_limit": 4,
              "max_path_hops": None, "protected_service_ids": []}
    bound = {"snapshot": s, "config": config, "authority": strong_tools.default_authority(s, config),
             "identity": {"workOrderId": "W1", "sourceSnapshotHash": "arrived-state", "policyHash": "policy"}}
    rebind(bound)
    return bound


def rebind(b):
    b["identity"].update(physicalStateHash=strong_tools.digest(b["snapshot"]), authorityHash=strong_tools.digest(b["authority"]))


def select_i(component, bound, symbol="I"):
    timing = {}
    catalog = component.catalog(bound, timing)
    action = next(a for a in catalog if a["id"] == symbol)
    return component.solve(action, [r["id"] for r in catalog], bound, timing)


class RuleContracts(unittest.TestCase):
    def test_historical_tools_search_only_authorized_work_after_release(self):
        for symbol in ("B", "C"):
            with self.subTest(symbol=symbol):
                frozen = make_bound()
                snap = frozen["snapshot"]
                shadow = {**deepcopy(snap["requests"]["one"]), "serviceId": "retained", "priority": 7}
                snap["requests"]["retained"] = shadow
                snap["assignments"]["retained"] = None
                # Its device reservation was deducted from observed capacity. OM grants only 'one'.
                frozen["authority"]["readableServiceIds"].append("retained")
                rebind(frozen)
                catalog = strong_tools.enumerate_actions(snap, frozen["authority"], frozen["config"])
                action = next(row for row in catalog if row["id"] == symbol)
                target, audit = strong_tools.solve_action(action, frozen, {})
                self.assertTrue(audit["valid"])
                self.assertIsNone(target["retained"])
                self.assertIsNotNone(target["one"])
                self.assertEqual(set(audit["pathSearch"]), {"one"})
                self.assertNotIn("rejectedCandidate", audit)

    def test_example_uses_different_feasible_route_on_the_same_i(self):
        b = make_bound()
        original = deepcopy(b)
        components = load_components(ROOT / "config/example-rules.json")
        chooser = SimpleNamespace(components=components, context={})
        rules = external_rules(chooser)
        self.assertIs(rules, external_rules(chooser))
        self.assertIsNone(external_rules(SimpleNamespace(components=None)))
        self.assertIsNone(external_rules(SimpleNamespace(components=load_components())))
        action = next(a for a in strong_tools.enumerate_actions(b["snapshot"], b["authority"], b["config"]) if a["id"] == "I")
        native_target, _ = strong_tools.solve_action(action, b, {})
        alternative, returned, checked = select_i(rules, b)
        self.assertEqual(native_target["one"]["linkIds"], ["fast"])
        self.assertEqual(alternative["one"]["linkIds"], ["wide"])
        self.assertEqual(alternative["one"]["allocatedBps"], 1000)
        self.assertEqual(b, original)
        self.assertTrue(checked["valid"])
        self.assertEqual(returned["allocator"], "residual-first bounded pair search")

    def test_external_input_mutations_cannot_redefine_the_current_budget_or_demand(self):
        class Mutator:
            def enumerate_actions(self, s, authority, config, history=None):
                rows = strong_tools.enumerate_actions(s, authority, config)
                authority["executableServiceIds"].clear()
                history["leaked"] = True
                return rows

            def solve_action(self, action, bound, context=None):
                bound["snapshot"]["requests"]["one"]["requirement"]["delayMs"] = 50
                path = {"nodes": ["a", "b"], "linkIds": ["late"], "allocatedBps": 1000}
                return bind_proposal(action, bound, {"one": path}, {})

        b = make_bound()
        original = deepcopy(b)
        rules = CheckedRules(Mutator(), SPEC, SYMBOLS)
        with self.assertRaisesRegex(ValueError, "proposal guard.*QOS_delayMs"):
            select_i(rules, b)
        self.assertEqual(b, original)
        self.assertNotIn("leaked", rules.context)

    def test_rejects_stale_identity_missing_roster_protected_writes_and_budget(self):
        b = make_bound()
        b["snapshot"]["requests"]["held"] = dict(deepcopy(b["snapshot"]["requests"]["one"]), serviceId="held")
        b["snapshot"]["assignments"]["held"] = None
        b["config"]["protected_service_ids"] = ["held"]
        b["authority"] = strong_tools.default_authority(b["snapshot"], b["config"])
        rebind(b)
        route = {"nodes": ["a", "b"], "linkIds": ["wide"], "allocatedBps": 1000}

        class Returning:
            enumerate_actions = staticmethod(strong_tools.enumerate_actions)

            def __init__(self, bad):
                self.bad = bad

            def solve_action(self, a, bound, context=None):
                target = deepcopy(bound["snapshot"]["assignments"])
                target["one"] = route
                audit = {}
                if self.bad == "roster":
                    del target["held"]
                elif self.bad == "scope":
                    target["held"] = route
                elif self.bad == "budget":
                    audit["localEvaluations"] = 4001
                result, audit = bind_proposal(a, bound, target, audit)
                if self.bad == "binding":
                    audit["binding"]["workOrderId"] = "another-work-order"
                return result, audit

        for bad, message in (("binding", "binding"), ("roster", "roster"), ("scope", "out-of-scope"),
                             ("budget", "local_evaluations")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                select_i(CheckedRules(Returning(bad), SPEC, SYMBOLS), b)
        b["config"]["t096_max_changed_services"] = 0
        with self.assertRaisesRegex(ValueError, "CHANGED_SERVICE_BUDGET"):
            select_i(CheckedRules(Returning("target"), SPEC, SYMBOLS), b)

    def test_catalog_authority_alias_version_and_inactive_i_cleanup(self):
        b = make_bound()
        rules = build_rules(config=SPEC)
        def substituted(s, authority, cfg, history=None):
            offered = strong_tools.enumerate_actions(s, authority, cfg)
            offered[0]["scope"]["serviceIds"] = ["one"]
            return offered
        rules.enumerate_actions = substituted
        with self.assertRaisesRegex(ValueError, "scope"):
            CheckedRules(rules, SPEC, SYMBOLS).catalog(b, {})
        aliases = dict(SPEC, options={"toolAliases": {"M": "I"}})
        with self.assertRaisesRegex(ValueError, "catalog version"):
            CheckedRules(rules, aliases, SYMBOLS + ("M",))
        aliases["catalogVersion"] = "example.alias.v2"
        mapping = CheckedRules(build_rules(config=aliases), aliases, SYMBOLS + ("M",))
        self.assertEqual(select_i(mapping, b, "M")[0]["one"]["linkIds"], ["wide"])
        b["snapshot"]["requests"]["one"]["_deleted"] = True
        b["snapshot"]["assignments"]["one"] = {"nodes": ["a", "b"], "linkIds": ["wide"], "allocatedBps": 1000}
        b["authority"] = strong_tools.default_authority(b["snapshot"], b["config"])
        rebind(b)
        target, raw, checked = select_i(mapping, b)
        self.assertEqual(target, {"one": None})
        self.assertTrue(checked["valid"])

    def test_optional_record_is_detached_from_inputs(self):
        b = make_bound()
        action = strong_tools.enumerate_actions(b["snapshot"], b["authority"], b["config"])[0]
        sample = begin_sample(SimpleNamespace(components=None), action, [action], b, {"cache": [1]})
        self.assertEqual(sample["action"], action)
        sample["bound"]["config"].clear()
        self.assertIn("path_limit", b["config"])


if __name__ == "__main__":
    unittest.main()
