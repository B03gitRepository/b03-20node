"""Regression vectors for both published scales and the sealed formula contract."""
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("src", "src/rules/vendor/src", "simulator/resnet_py/src")]
import b03_formulas as f
from resnet.control.policy import _quality, required_wire_bps


class FormulaVectors(unittest.TestCase):
    def test_integer_wire_rate_and_units(self):
        for payload in [0, 1, 25, 249, 250, 1000, 2_400_000, 2**54 + 11]:
            self.assertEqual(f.wire_bps(payload), required_wire_bps(payload))
        self.assertEqual(f.wire_bps(2_400_000), 2_486_400)
        self.assertEqual(f.wire_bps(1), 2)
        self.assertEqual(f.ns_to_ms(2_500_000), 2.5)
        self.assertEqual(f.seconds_to_ns(.0000000025), 2)

    def test_qos_profiles_retain_original_boundary(self):
        rows = [dict(delayMs=1,jitterMs=3,ber=.6,packetLossRate=.1,bandwidthBps=90),
                dict(delayMs=2,jitterMs=4,ber=.6,packetLossRate=.2,bandwidthBps=80)]
        q = f.path_metrics(rows)
        self.assertEqual((q["delayMs"], q["jitterMs"], q["ber"], q["bottleneckBps"]), (3,5,1,80))
        self.assertAlmostEqual(q["packetLossRate"], .28)
        link = {str(i):dict(delay_ns=x["delayMs"]*10**6,jitter_ns=x["jitterMs"]*10**6,
                             ber=x["ber"],packet_loss_rate=x["packetLossRate"],capacity_bps=x["bandwidthBps"])
                for i,x in enumerate(rows)}
        service=SimpleNamespace(delay_limit_s=10,jitter_limit_s=10,loss_limit=1,ber_limit=1)
        native, violations = _quality(service,list(link),link,1036,qos_profile="official")
        observed, rejected = f.packet_quality(service,list(link),link,qos_profile="official")
        self.assertEqual((observed, rejected), (native, violations))
        self.assertEqual(observed["configured_ber_sum"], 1.2)
        self.assertIn("ber_metadata_limit",violations)
        self.assertIsNone(f.path_metrics([])["bottleneckBps"])

    def test_preexisting_overload_and_device_limit(self):
        self.assertTrue(f.within_transition(805,804,805))
        self.assertFalse(f.within_transition(806,804,805))
        self.assertEqual(f.capacity_budget(101,.05),95)
        self.assertEqual(f.capacity_budget(100,.29),71)
        self.assertFalse(f.within_capacity(805,804))

    def test_loss_integrals_and_empty_denominators(self):
        active={"first":4,"second":1}
        self.assertEqual(f.service_losses(7,["first"],active),(7,28))
        self.assertEqual(f.service_losses(0,["first"],active),(0,0))
        self.assertIsNone(f.ratio(0,0))
        self.assertEqual(f.latency_distribution([20,None,0,10])["p95Ns"],20)
        self.assertEqual(f.latency_distribution(range(1,21))["p95Ns"],19)
        self.assertIsNone(f.latency_distribution([])["p95Ns"])

    def test_immutable_edition_and_query(self):
        self.assertEqual(f.lookup(identifier="packet.wire-rate")[0]["version"],"1.0.0")
        self.assertTrue(f.lookup(topic="容量"))
        modified=f.lookup(identifier="packet.wire-rate")[0]
        modified["parameters"]["epochBytes"]=1
        self.assertEqual(f.wire_bps(1000),1036)
        data=json.loads(f.EDITION_BYTES)
        data["entries"][0]["operation"]="unregistered"
        with self.assertRaises(ValueError):
            f.validate_edition(json.dumps(data).encode())

    def test_receipt_rejects_changed_effective_parameters(self):
        conf={"payload_bytes":1000,"control":{"control_headroom_fraction":.05,"qos_profile":"official",
              "observation_ttl_ns":10**10,"command_validity_ns":5*10**9}}
        impl={"src/b03_formulas.py":"a"*64,"knowledge/formulas-v1.json":f.EDITION_SHA256}
        binding=f.receipt_binding(conf,impl)
        f.verify_binding(binding,f.EDITION_BYTES,conf,impl)
        for key in ("editionSha256","implementationSha256","version"):
            bad=deepcopy(binding);bad[key]="bogus"
            with self.assertRaises(ValueError):
                f.verify_binding(bad,f.EDITION_BYTES,conf,impl)
        altered=deepcopy(conf);altered["control"]["observation_ttl_ns"]+=1
        with self.assertRaises(ValueError):
            f.verify_binding(binding,f.EDITION_BYTES,altered,impl)
