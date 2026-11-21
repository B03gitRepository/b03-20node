"""The deferred cleanup proof must protect a retained epoch without the simulator."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/packet"))
from packet_cleanup_evidence import cleanup_budgets


class DeferredCleanupTests(unittest.TestCase):
    def setUp(self):
        self.cond = {"deviceRecovery": {"contractVersion": "b03.deferred-cleanup.v1", "deferUnreachableCleanup": True},
                     "simulation": {"control":{"command_validity_ns":3,"transaction_timeout_ns":15,"observation_ttl_ns":8}}}
        self.choice={"service_id":"old","node_path":["source","lost"],"link_path":["one"],"wire_bps":7}
        def event(name,t,**fields):
            return {"event":name, "time_ns":t, "work_id":"first", **fields}
        self.rows=[
            event("work_started",0,release_ids=["old"],install_ids=[]),
            event("work_step",1,phase="release",service_id="old",plan_id="plan",decision=self.choice,retained_wire_bps={"one":7}),
            event("device_ack",2,op="deactivate",plan_id="plan",node_id="source",service_id="old",epoch=1,original_status="applied"),
            event("native_transaction",2,phase="source_stop_ack",plan_id="plan",reconciled_by_query=False),
            event("device_command",3,op="cleanup",service_id="old",command_id="c",node_id="lost",epoch=1,issued_at_ns=3,plan_id="plan",retry=False),
            event("native_command_timeout",14,operation="query",plan_id="plan"),
            event("cleanup_deferred",18,expired_command_id="c",epoch=1,node_id="lost",protected_services=["old"],
                  protected_nodes=["lost"],protected_links=["one"],retained_wire_bps={"one":7})]
        self.probes={}

    def validate(self):
        groups=defaultdict(lambda:defaultdict(list))
        for row in self.rows:
            groups[row.get("work_id")][row["event"]].append(row)
        return cleanup_budgets(self.rows,groups,self.cond,self.probes)

    def test_unresolved_epoch_remains_reserved(self):
        self.assertEqual(self.validate(),{})
        self.rows[-1]["retained_wire_bps"]={}
        with self.assertRaisesRegex(ValueError,"wire changed"):
            self.validate()

    def test_requires_opt_in_and_device_ack(self):
        self.cond["deviceRecovery"]["deferUnreachableCleanup"]=False
        with self.assertRaisesRegex(ValueError,"not selected"):
            self.validate()
        self.cond["deviceRecovery"]["deferUnreachableCleanup"]=True
        self.rows[2]["original_status"]="rejected"
        with self.assertRaisesRegex(ValueError,"source-stop acknowledgment"):
            self.validate()

    def test_unrelated_work_cannot_use_held_port_or_link(self):
        after={"event":"work_step","time_ns":19,"work_id":"second","service_id":"new",
               "decision":{"node_path":["third","fourth"],"link_path":["other"],"wire_bps":1}}
        self.rows.append(after)
        self.assertEqual(self.validate(),{})
        self.rows[-1]["decision"]["link_path"]=["one"]
        with self.assertRaisesRegex(ValueError,"conflicting foreign work"):
            self.validate()

    def test_recovery_requires_a_delivered_new_probe_and_readback(self):
        wake={"event":"cleanup_recovery_probe","work_id":"first","time_ns":25,"epoch":1,"node_id":"lost",
              "link_id":"one","seq":5,"sampled_at_ns":22,"arrived_at_ns":23}
        self.rows.append(wake)
        with self.assertRaisesRegex(ValueError,"delivered device probe"):
            self.validate()
        self.probes["one","lost",5]={"time_ns":23,"probe":{"observed_at_ns":22},"result":{"accepted":True}}
        self.rows.append({"event":"device_command","work_id":"first","time_ns":25,"op":"cleanup","node_id":"lost",
                          "epoch":1,"service_id":"old","command_id":"retry-write"})
        with self.assertRaisesRegex(ValueError,"before reconciliation"):
            self.validate()


if __name__=="__main__":
    unittest.main()
