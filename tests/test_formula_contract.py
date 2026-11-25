"""Cross-contract and deployment regressions for the normative formula edition."""
import itertools
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import b03_formulas as formula
from integrity import check_members, receipt_format, seal


class BoundaryTests(unittest.TestCase):
    def test_pin_native_defaults_and_zero_hot_path_reads(self):
        cfg = json.loads((ROOT / "simulator/resnet_py/configs/common.json").read_bytes())["simulation"]
        control = cfg["control"]
        self.assertEqual((cfg["payload_bytes"], control["control_headroom_fraction"],
                          control["observation_ttl_ns"], control["command_validity_ns"]),
                         (formula.PAYLOAD_BYTES, formula.DEFAULT_HEADROOM, formula.DEFAULT_TTL_NS,
                          formula.DEFAULT_VALIDITY_NS))
        with patch.object(Path, "read_bytes", side_effect=AssertionError("hot-path file read")), \
                patch.object(json, "loads", side_effect=AssertionError("hot-path JSON parse")):
            for _ in range(32):
                self.assertEqual(formula.wire_bps(1000), 1036)
                self.assertTrue(formula.within_transition(23, 23, 12))
                self.assertEqual(formula.path_metrics([])["packetLossRate"], 0)
                self.assertTrue(formula.lookup(identifier="path.qos.packet"))

    def test_endpoint_fusion_freshness_and_missing_evidence_clocks(self):
        left = dict(capacity_bps=90, delay_ns=7, jitter_ns=2, ber=.2, packet_loss_rate=.3)
        right = dict(capacity_bps=100, delay_ns=4, jitter_ns=3, ber=.3, packet_loss_rate=.2)
        expected = dict(capacity_bps=90, delay_ns=7, jitter_ns=3, ber=.3, packet_loss_rate=.3)
        self.assertEqual(formula.fuse_endpoints(left, right), expected)
        self.assertEqual(formula.fuse_endpoints(right, left), expected)
        cases = [(0, 3, 10, True), (0, 3, 11, False), (5, 4, 10, False), (2, 11, 10, False)]
        for sampled, arrived, now, valid in cases:
            self.assertIs(formula.fresh_sample(sampled, arrived, now, 10), valid)
        self.assertIsNone(formula.elapsed_ns(None, 4))
        self.assertIsNone(formula.elapsed_ns(3, None))
        self.assertEqual(formula.elapsed_ns(5, 13), 8)

    def test_seeded_path_and_search_vectors(self):
        rng = random.Random(331)
        for _ in range(100):
            path = [dict(delayMs=rng.randrange(20), jitterMs=rng.randrange(8), ber=rng.random(),
                         packetLossRate=rng.random(), bandwidthBps=rng.randrange(1, 500))
                    for _ in range(rng.randint(1, 8))]
            delay = sum(row["delayMs"] for row in path)
            jitter = math.sqrt(sum(row["jitterMs"] ** 2 for row in path))
            ber = min(1, sum(row["ber"] for row in path))
            survival = math.prod(1 - row["packetLossRate"] for row in path)
            limit = dict(delayMs=delay, jitterMs=jitter, ber=ber, packetLossRate=1 - survival)
            self.assertEqual(formula.path_metrics(path), dict(limit, bottleneckBps=min(r["bandwidthBps"] for r in path)))
            state = (0, 0, 0, 1)
            for row in path:
                state = formula.extend_path(*state, row)
            self.assertFalse(formula.exceeds_qos(state, limit))
            self.assertEqual((state[0], state[2], state[3]), (delay, ber, survival))
            self.assertAlmostEqual(state[1], jitter**2)
            self.assertEqual(formula.search_vector(path[0])[:3],
                             (path[0]["delayMs"], path[0]["jitterMs"]**2, path[0]["ber"]))
        limits = formula.search_limits(dict(delayMs=1, jitterMs=2, ber=1, packetLossRate=1), 3)
        self.assertGreater(limits[0], 1)
        self.assertGreater(limits[1], 4)
        self.assertEqual(limits[2:], (math.inf, math.inf))

    def test_single_ulp_search_boundaries_and_native_wire_keywords(self):
        jitter = 812079.5283621605
        link = dict(delayMs=0, jitterMs=jitter, ber=0, packetLossRate=0)
        self.assertEqual(formula.search_vector(link)[1], jitter*jitter)
        squared = 886927.0226647181
        limit = dict(delayMs=0, jitterMs=math.sqrt(squared) - formula.EPS, ber=0, packetLossRate=0)
        state = (0, squared, 0, 1)
        # libm may return equal rounded roots on some platforms. Compare to that
        # platform's original T140 operations, including their threshold.
        threshold = limit["jitterMs"] + formula.EPS
        self.assertEqual(formula.exceeds_qos(state, limit, formula.EPS, jitter_root=formula.widest_rule_jitter),
                         squared**.5 > threshold)
        self.assertEqual(formula.exceeds_qos(state, limit, formula.EPS), math.sqrt(squared) > threshold)
        self.assertEqual(formula.wire_bps(1000, payload_bytes=1000, epoch_overhead_bytes=8), 1036)

    def test_once_iterable_sequence_subclasses(self):
        row = dict(delayMs=7, jitterMs=3, ber=.25, packetLossRate=.125, bandwidthBps=200)
        expected = dict(delayMs=7, jitterMs=3., ber=.25, packetLossRate=.125, bottleneckBps=200)
        for base in (list, tuple):
            class OnceIterable(base):
                def __iter__(self):
                    if not hasattr(self, "cursor"):
                        self.cursor = super().__iter__()
                    return self.cursor

            with self.subTest(base=base.__name__):
                self.assertEqual(formula.path_metrics(OnceIterable([row])), expected)

    def test_priority_ranking_and_change_tiebreak(self):
        requests = {str(i): {"priority": level} for i, level in enumerate([1, 1, 2, 3, 3])}
        current = {key: "old" if key == "1" else None for key in requests}
        weights = formula.priority_coefficients(row["priority"] for row in requests.values())
        rows = []
        for active in itertools.product([False, True], repeat=len(requests)):
            target = {key: "old" if bit else None for key, bit in zip(requests, active)}
            changed = sum(target[key] != current[key] for key in requests)
            scalar = sum(weights[requests[key]["priority"]] for key in requests if target[key]) - changed
            rows.append((formula.priority_objective(requests, target, current), scalar))
        for a, b in itertools.product(rows, repeat=2):
            self.assertEqual((a[0] > b[0], a[0] == b[0]), (a[1] > b[1], a[1] == b[1]))

    def test_large_integer_clocks_replay_t140_returns(self):
        from packet_learner import unweighted_advantage
        from packet_training import measured_u

        ns = 2**53 + 3
        credit = dict(script="04", nodes=100, fromNs=0, toNs=ns,
                      actualUnmetServiceNs=ns, fixedIUnmetServiceNs=ns + 2,
                      rawReturnSeconds=-ns / 1e9, fixedIReturnSeconds=-(ns + 2) / 1e9)
        self.assertEqual(unweighted_advantage(credit), 2 / 1e9)
        self.assertEqual(formula.ns_to_seconds(ns), ns / 1e9)
        self.assertEqual(formula.ns_to_ms(ns), ns / 1_000_000)
        self.assertEqual(formula.archived_ns_to_ms(ns), ns / 1e6)
        self.assertEqual(formula.seconds_to_ns(ns), round(ns * 1e9))
        self.assertEqual(formula.ms_to_ns(.0000015), .0000015 * 1_000_000)
        self.assertEqual(formula.seconds_to_us(12), 12_000_000)
        self.assertEqual(measured_u([dict(start_ns=0, end_ns=ns, unmet=["a","b"])], 1, ns), (ns-1)*2)
        self.assertEqual(math.copysign(1, formula.ns_to_seconds(-0)), 1)

    def test_time_fractions_and_priority_loss(self):
        valid = [formula.time_fraction(4, None), formula.time_fraction(3, 1), formula.time_fraction(1, 0)]
        self.assertEqual(formula.ratio(sum(v for v, _ in valid), sum(t for _, t in valid)), .75)
        self.assertEqual([formula.online_pair_count(n) for n in (0, 1, 2, 10)], [0, 0, 2, 90])
        self.assertEqual(formula.service_losses(17, ("low", "high"), {"low": 1, "high": 4}), (34, 85))

    def test_new_v3_requires_both_pins_and_sealed_member(self):
        pins = {formula.IMPLEMENTATION: "a" * 64, formula.DECLARATION: formula.EDITION_SHA256}
        conditions = {"mode": "workorder", "script": "demo", "formulaBinding": {},
                      "implementationSha256": pins, "receiptContract": "b03.packet.workorder.v3"}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("SUMMARY.json", "b03.jsonl", "business-intervals.jsonl", "input/net-topo.json",
                         "input/bg-service.json", "input/scripts/script-demo.json"):
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("{}")
            (root / "CONDITIONS.json").write_text(json.dumps(conditions))
            seal(root)
            with self.assertRaisesRegex(ValueError, "missing"):
                check_members(root, conditions)
            (root / "RUN_FILES.json").unlink()
            (root / formula.MEMBER).write_bytes(formula.EDITION_BYTES)
            seal(root)
            self.assertIn(formula.MEMBER, check_members(root, conditions))
            (root / formula.MEMBER).write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                check_members(root, conditions)
        for missing in pins:
            remaining = {k:v for k,v in pins.items() if k != missing}
            with self.assertRaises(ValueError):
                receipt_format(dict(conditions, implementationSha256=remaining))
        old = dict(mode="workorder", implementationSha256={}, receiptContract="b03.packet.workorder.v2")
        self.assertEqual(receipt_format(old), "b03.packet.run-files.v2")
        with self.assertRaises(ValueError):
            receipt_format(dict(old, provenance={"checkoutFiles": pins}))
        self.assertEqual(receipt_format(dict(mode="workorder")), "b03.packet.run-files.v1")

    def test_isolated_export_and_external_rule(self):
        source = ROOT / "collab/rules/check.py"
        with tempfile.TemporaryDirectory() as temp:
            kit = Path(temp) / "kit"
            args = [sys.executable, "-I", "-B"]
            exported = subprocess.check_output([*args, str(source), "--export-kit", str(kit)])
            self.assertEqual(len(json.loads(exported)["files"]), 5)
            check = [*args, "collab/rules/check.py"]
            golden = json.loads(subprocess.check_output(check, cwd=kit))
            provider = Path(temp) / "local_rule.py"
            provider.write_text((kit / "src/collab_patch_rule.py").read_text(), encoding="utf-8")
            actual = json.loads(subprocess.check_output([*check, "--provider", str(provider)], cwd=kit))
            self.assertEqual(golden, actual)
            refused = subprocess.run([*args, str(source), "--export-kit", str(kit)], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
