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
        ports.observe("meter", self.case["meter"])
        with self.assertRaises(ValueError):
            ports.finish(self.case["drainEndNs"])

    def test_terminal_evidence_cannot_change_next_probe_or_resume_a_closed_run(self):
        seen = []
        def watch(channel):
            def receive(data):
                seen.append(channel)
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    ports.event(self.case["event"])
            return receive

        detector = self.provider.on_event
        self.provider.on_meter = watch("meter")
        self.provider.on_device = watch("device")
        self.provider.on_event = lambda event: [] if seen else detector(event)
        ports = Ports(self.provider)
        ports.observe("device", self.case["device"][0])
        ports.observe("meter", self.case["meter"])
        ports.observe("device", self.case["device"][1])
        self.assertEqual(ports.event(self.case["event"]), [("fern", 7000000), ("moss", 7000000)])
        self.assertEqual(seen, [])
        ports.finish(self.case["drainEndNs"])
        self.assertEqual(seen, ["device", "meter", "device"])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            ports.event(self.case["event"])
        with self.assertRaises(RuntimeError):
            ports.finish(self.case["drainEndNs"])

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
            delivered, visible_batches = [], []
            detector = sim.ports.provider
            detect = detector.on_event
            def select_probes(event):
                visible_batches.append(event)
                # If either offline channel were exposed early, this stateful policy would cancel the probes.
                return [] if delivered else detect(event)
            detector.on_event = select_probes
            def watch(channel, callback):
                def receive(record):
                    delivered.append((channel, sim.now))
                    callback(record)
                return receive
            detector.on_device = watch("device", detector.on_device)
            detector.on_meter = watch("meter", detector.on_meter)
            try:
                sim.start_meter(run, solver)
                try:
                    sim.advance(sim.end_ns)
                    self.assertFalse(delivered)
                    with self.assertRaisesRegex(RuntimeError, "drain"):
                        sim.finish_meter()
                    sim.advance(sim.stop_ns)
                    self.assertFalse(delivered)
                    sim.finish_meter()
                    self.assertTrue(delivered)
                    self.assertEqual({at for _, at in delivered}, {sim.stop_ns})
                    self.assertEqual({kind for kind, _ in delivered}, {"device", "meter"})
                    self.assertEqual([x["sequence"] for x in visible_batches], [1])
                    self.assertEqual([x["type"] for x in visible_batches[0]["changes"]], ["link_state"])
                    with self.assertRaisesRegex(RuntimeError, "finished"):
                        sim.advance(sim.stop_ns)
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
            summary = {"clock": {"start_ns": 0, "end_ns": sim.end_ns, "drain_end_ns": sim.stop_ns},
                       "simulatorPorts": sim.ports.receipt()}
            verify_ports(run, conditions, summary, controller, executed, intervals)
            self.assertTrue(all(x > 0 for x in summary["simulatorPorts"]["callbacks"].values()))
            original = intervals[0]["W_service_ns"]
            intervals[0]["W_service_ns"] += 1
            with self.assertRaisesRegex(ValueError, "differ from sealed"):
                verify_ports(run, conditions, summary, controller, executed, intervals)
            intervals[0]["W_service_ns"] = original
            summary["simulatorPorts"]["callbacks"]["device"] -= 1
            with self.assertRaisesRegex(ValueError, "differ from sealed"):
                verify_ports(run, conditions, summary, controller, executed, intervals)
