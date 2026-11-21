"""Exercise expiry and readmission with the pinned packet engine and real OM/RC endpoints."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("src", "src/packet", "src/rules", "src/rules/vendor/src")]
if simulator := os.environ.get("B03_SIMULATOR"):
    sys.path.insert(0, str(Path(simulator) / "src"))


class BOnly:
    def enumerate_actions(self, snapshot, authority, config, history=None):
        from strong_tools import enumerate_actions
        if any(snapshot["requests"][sid]["_deleted"] for sid in authority["executableServiceIds"]):
            raise AssertionError("Controller expiry reached the rule provider")
        return [a for a in enumerate_actions(snapshot, authority, config, history) if a["id"] == "B"]

    def solve_action(self, action, bound, context=None):
        from rule_component import bind_proposal
        from strong_tools import solve_action
        assert action["id"] == "B"
        return bind_proposal(action, bound, *solve_action(action, bound, context))


def build_rules(*, config):
    return BOnly()


def contention_scenario():
    from resnet.contracts.scenario import EventBatch, LinkSpec, NodeSpec, ScenarioSpec, ServiceSpec

    nodes = tuple(NodeSpec(n, "test", "", "", {}, {}) for n in ("x", "y", "z"))
    links = tuple(LinkSpec(a+b, "test", a, b, 10_000_000, .0001, 0, 0, 0, {})
                  for a, b in (("x", "y"), ("y", "z")))
    def service(sid, a, b, priority, bps):
        return ServiceSpec(sid, "test", "test", a, b, priority, bps, .05, .005, .001, .01, 0, {})

    services = (service("expires", "x", "y", 5, 8_000_000), service("waiting", "x", "y", 3, 6_000_000),
                service("healthy", "y", "z", 4, 1_000_000), service("protected", "y", "z", 5, 1_000_000))
    roster = [s.service_id for s in services]
    batches = tuple(EventBatch(at, seq, ({"kind": "traffic_set", "active_service_ids": active},),
                               None, tuple(active))
                    for seq, (at, active) in enumerate(((0, roster), (8_000_000, roster[1:]))))
    return ScenarioSpec("single-expiry", 22, nodes, links, services, (), (), batches, (), {}, {})


@unittest.skipUnless(simulator, "Set B03_SIMULATOR to run the native packet lifecycle regression")
class PacketLifecycle(unittest.TestCase):
    def test_release_confirmed_before_waiting_service_reuses_wire(self):
        from frozen_policy import DecisionPolicy
        from packet_device import install
        from packet_roles import PacketRoles, _configuration, _diagnose, _model
        from resnet.simulation.simulation import Simulation

        scenario = contention_scenario()
        ids = [s.service_id for s in scenario.services]
        cfg = {"mode": "control", "end_s": 20, "drain_s": 10,
               "control": {"controller_id": "x", "qos_profile": "official"}}
        from component_config import load_components
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        manifest = Path(folder.name) / "b-only.json"
        manifest.write_text(json.dumps({"extends": str(ROOT / "config/components.json"),
            "rules": {"id": "test.b-only", "version": "1", "entrypoint": "test_packet_lifecycle:build_rules"}}),
            encoding="utf-8")
        provider = load_components(manifest).activate("B")
        chooser = DecisionPolicy("B", components=provider)
        chooser.record_component_io = True
        events = []
        demand_index = {}

        class TracedSimulation(Simulation):
            def log(self, name, row):
                if name == "controller" and row["event"] == "service_observation" and row["result"]["accepted"]:
                    msg = row["message"]
                    demand_index[msg["service"]["service_id"], msg["seq"]] = row
                super().log(name, row)

        sim = TracedSimulation(scenario, cfg)
        self.addCleanup(sim.close)
        with PacketRoles(ids, events.append, protected_ids=["protected"], chooser=chooser) as roles:
            loop = install(sim, roles, events.append)
            sim.advance(7_000_000_000)
            self.assertEqual(set(loop.confirmed), {"expires", "healthy"})
            old = deepcopy(loop.confirmed["expires"])
            healthy_epoch = loop.confirmed["healthy"]["epoch"]
            sim.advance(19_000_000_000)
            self.assertEqual(set(loop.confirmed), {"healthy", "waiting"})
            self.assertEqual(loop.confirmed["healthy"]["epoch"], healthy_epoch)
            self.assertEqual(sim.sources["waiting"].rate_bps, 6_000_000)
            sim.advance(30_000_000_000)
            self.assertEqual(loop.budgets(), {})

        def rows(event, **fields):
            return [r for r in events if r["event"] == event and all(r.get(k) == v for k, v in fields.items())]

        plans = rows("b03_rc_plan")
        release = next(p for p in plans if p.get("lifecycle_release", {}).get("deferred_active_ids") == ["waiting"])
        wid = release["work_id"]
        self.assertEqual(release["authorized"], ["expires"])
        self.assertEqual(release["release_ids"], ["expires"])
        self.assertEqual(release["install_ids"], [])
        lifecycle_plans = [p for p in plans if "lifecycle_release" in p]
        self.assertGreaterEqual(len(lifecycle_plans), 2)
        for p in lifecycle_plans:
            self.assertEqual(p["tool_choice"], {"method": "lifecycle", "selected": None,
                                               "selectionWallNs": 0, "modelCalled": False})
            self.assertEqual((p["solver_wall_ns"], p["real_solver_calls"]), (0, 0))
            self.assertEqual(p["decision_wall_ns"], p["lifecycle_wall_ns"])
            self.assertGreater(p["lifecycle_wall_ns"], 0)
            self.assertNotIn("component_io", p)
            self.assertIsNone(p["solver_audit"]["action"])
        for p in plans:
            if p["real_solver_calls"]:
                self.assertEqual([a["id"] for a in p["component_io"]["catalog"]], ["B"])
                self.assertEqual(p["decision_wall_ns"], p["solver_wall_ns"] + p["tool_choice"]["selectionWallNs"]
                                 + p["component_timing"]["chargedComponentWallNs"])

        step, = rows("work_step", work_id=wid)
        requested, = rows("readback_requested", work_id=wid, phase="release")
        returned, = rows("service_released", work_id=wid)
        verified, = rows("work_verified", work_id=wid)
        original_wire = old["decision"]["wire_bps"]
        self.assertEqual([r["retained_wire_bps"]["xy"] for r in (step, requested)], [original_wire]*2)
        self.assertEqual(returned["retained_wire_bps"].get("xy", 0), 0)
        path_reads = rows("device_readback", work_id=wid, phase="release")
        self.assertEqual({r["node_id"] for r in path_reads}, set(old["decision"]["node_path"]))
        for read in path_reads:
            self.assertTrue(read["matched"] and read["result"]["retired"])
            self.assertEqual(read["query_id"], returned["query_ids"][read["node_id"]])
            self.assertLessEqual(read["arrived_at_ns"], returned["time_ns"])
        self.assertFalse(rows("device_command", service_id="protected"))
        self.assertEqual(rows("release_authority_checked", work_id=wid)[0]["status"], "reported_inactive")
        event, = rows("b03_observation_evidence", work_id=wid, purpose="plan")
        self.assertFalse(event["state"]["services"]["expires"]["active"])
        self.assertLessEqual(event["state"]["services"]["expires"]["received_at_ns"], release["time_ns"])
        self.assertTrue(event["state"]["confirmed"]["expires"]["configuration_evidence"]["verified"])

        followup = plans[plans.index(release) + 1]
        self.assertEqual(followup["reassessment_after_release"],
                         {"work_id": wid, "verified_at_ns": verified["time_ns"], "release_ids": ["expires"]})
        self.assertLess(verified["time_ns"], followup["time_ns"])
        self.assertEqual(followup["tool_choice"]["selected"], "B")
        self.assertEqual(followup["install_ids"], ["waiting"])
        after, = rows("b03_observation_evidence", work_id=followup["work_id"], purpose="plan")
        self.assertNotIn("expires", after["state"]["confirmed"])
        self.assertEqual(after["state"]["retained_wire_bps"].get("xy", 0), 0)
        self.assertLessEqual(after["state"]["services"]["waiting"]["received_at_ns"], after["time_ns"])
        self.assertTrue(rows("work_verified", work_id=followup["work_id"]))
        final = plans[plans.index(lifecycle_plans[-1]) + 1]
        self.assertEqual(final["release_ids"] + final["install_ids"], [])
        self.assertTrue(rows("b03_observation_evidence", work_id=final["work_id"], purpose="plan"))

        from lifecycle_evidence import check_lifecycle
        conditions = {"authority": {"allowedServiceIds": ids, "protectedServiceIds": ["protected"]},
                      "simulation": {"control": sim.control.options}}
        checks = check_lifecycle(plans, events, demand_index, conditions)
        self.assertEqual(checks, {"releaseOrders": len(lifecycle_plans), "confirmedReleases": 3,
                                  "followupOrders": len(lifecycle_plans), "activeServicesReassessed": 1,
                                  "pendingReleaseOrders": 0, "pendingReassessments": 0})
        waiting_msg = demand_index["waiting", after["state"]["services"]["waiting"]["seq"]]["message"]
        for item, change in [(release["tool_choice"], {"selected": "I"}),
                             (returned, {"retained_wire_bps": {"xy": original_wire}}),
                             (followup, {"reassessment_after_release": None}), (waiting_msg, {"active": False})]:
            with self.subTest(corruption=change), patch.dict(item, change), self.assertRaises(ValueError):
                check_lifecycle(plans, events, demand_index, conditions)

        for condition in ("protected", "outside_whitelist", "stale", "unverified"):
            with self.subTest(condition=condition):
                state = deepcopy(event["state"])
                protect = {"expires"} if condition == "protected" else set()
                allowed = set(ids) - ({"expires"} if condition == "outside_whitelist" else set())
                if condition == "stale":
                    state["services"]["expires"]["fresh"] = False
                if condition == "unverified":
                    state["confirmed"]["expires"]["configuration_evidence"]["verified"] = False
                model, ledger = _model(state, .05, allowed-protect)
                diagnosis = _diagnose(state, model, ledger, _configuration(protect, model), allowed, protect)
                self.assertNotIn("lifecycle_release", diagnosis)
                self.assertFalse(diagnosis["services"]["expires"]["authorized"])
                self.assertEqual(ledger["reported_wire_bps"]["xy"], original_wire)


class LegacyLifecycleEvidence(unittest.TestCase):
    def test_historical_release_does_not_require_a_new_schema(self):
        from lifecycle_evidence import check_lifecycle
        old = {"event": "b03_rc_plan", "release_ids": ["old"], "tool_choice": {"method": "lifecycle", "selected": "I"}}
        self.assertFalse(any(check_lifecycle([old], [{"event": "b03_runtime"}], {}, {}).values()))
