"""The simulator extension receives detached records; probe scheduling stays bounded and causal."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from simulator_ports import Ports, VERSION, verify_ports
from example_public_simulator import build


class PublicPorts(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / "collab/simulator/case.json").read_bytes())
        self.provider = build(self.case["settings"])

    def test_rejects_actions_outside_physical_sensing(self):
        for response in ([{"op": "install", "nodeId": "fern", "delayNs": 10}],
                         [{"op": "probe", "nodeId": "other", "delayNs": 10}],
                         [{"op": "probe", "nodeId": "fern", "delayNs": 0}],
                         [{"op": "probe", "nodeId": "fern", "delayNs": self.case["event"]["remainingNs"]}],
                         [self.case["expectedRequests"][0]] * 2):
            self.provider.on_event = lambda event: response
            with self.subTest(response=response), self.assertRaises(ValueError):
                Ports(self.provider).event(self.case["event"])

    def test_delivered_and_measured_records_are_read_only(self):
        original = deepcopy(self.case["observation"])
        def observer(value):
            value["message"]["version"] = 99
        self.provider.on_observation = observer
        ports = Ports(self.provider)
        ports.observe("observation", self.case["observation"])
        self.assertEqual(self.case["observation"], original)
        self.provider.on_meter = lambda value: {"weightedUnmetServiceNs": 0}
        with self.assertRaises(ValueError):
            ports.observe("meter", self.case["meter"])

    def test_native_probe_goes_through_delivery_and_its_receipt_replays(self):
        from test_packet_lifecycle import contention_scenario
        from component_config import load_components
        from simulator_component import simulation_type
        from resnet.contracts.scenario import EventBatch
        from b03 import solver

        scenario = contention_scenario()
        change = {"kind": "official_event", "payload": {"eventType": "link_state",
                                                          "attrs": {"linkId": "xy", "state": "up"}}}
        batch = EventBatch(3_300_000, 99, (change,), None, scenario.batches[0].active_service_ids)
        scenario = replace(scenario, batches=tuple(sorted((*scenario.batches, batch), key=lambda b: b.time_us)))
        cfg = {"mode": "control", "end_s": 10, "drain_s": 1, "control": {"controller_id": "x", "qos_profile": "official"}}
        registered = load_components(ROOT / "config/collab-simulator.json").activate("single")
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            sim = simulation_type(registered)(scenario, cfg, run)
            try:
                sim.start_meter(run, solver)
                try:
                    sim.advance(sim.stop_ns)
                    sim.finish_meter()
                finally:
                    sim.close_meter()
            finally:
                sim.close()
            def records(name):
                return [json.loads(line) for line in (run / name).read_bytes().splitlines()]
            controller = records("controller.jsonl")
            executed = records("executions.jsonl")
            intervals = records("business-intervals.jsonl")
            trace = records("simulator-events.jsonl")
            self.assertEqual(sum(row["event"] == "probe_sampled" for row in trace), 2)
            self.assertTrue(any(row["event"] == "observation" and row["probe"]["observed_at_ns"] == 3_500_000_000
                                and row["time_ns"] >= row["probe"]["observed_at_ns"] for row in controller))
            (run / "input").mkdir()
            (run / "input/net-topo.json").write_text(json.dumps({"links": [
                {"linkId": link.link_id, "srcNodeId": link.src_node_id, "dstNodeId": link.dst_node_id}
                for link in scenario.links]}), encoding="utf-8")
            (run / "RUN_FILES.json").write_text(json.dumps({"files": [f.name for f in run.glob("*.jsonl")]}))
            conditions = {"components": {"simulator": {"contractVersion": VERSION}}}
            summary = {"clock": {"start_ns": 0, "end_ns": sim.end_ns}, "simulatorPorts": sim.ports.receipt()}
            verify_ports(run, conditions, summary, controller, executed, intervals)
            self.assertTrue(all(x > 0 for x in summary["simulatorPorts"]["callbacks"].values()))
            summary["simulatorPorts"]["callbacks"]["device"] -= 1
            with self.assertRaisesRegex(ValueError, "counts differ"):
                verify_ports(run, conditions, summary, controller, executed, intervals)
