"""The deferred cleanup proof must protect a retained epoch without the simulator."""
from collections import defaultdict
import os
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
            event("device_command",8,op="query",service_id="old",command_id="q",node_id="lost",epoch=1,issued_at_ns=8,plan_id="plan",retry=False),
            event("native_command_timeout",14,operation="query",plan_id="plan",command_id="q"),
            event("cleanup_deferred",18,expired_command_id="c",timed_out_query_id="q",epoch=1,node_id="lost",protected_services=["old"],
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

    def test_wrong_query_timeout_cannot_release_the_device_slot(self):
        self.rows[-1]["timed_out_query_id"]="unrelated-query"
        with self.assertRaisesRegex(ValueError,"timed-out read-only"):
            self.validate()

    @unittest.skipUnless(os.environ.get("B03_SIMULATOR"), "Set B03_SIMULATOR for native controller behavior")
    def test_native_unknown_without_a_matching_query_timeout_stays_attached(self):
        sys.path.insert(0,str(Path(os.environ["B03_SIMULATOR"]) / "src"))
        from packet_device import WorkLoop
        loop=object.__new__(WorkLoop)
        old={"decision":self.choice,"epoch":1}
        original={"command_id":"c","issued_at_ns":3,"validity_ns":3,"target_node_id":"lost",
                  "action":{"op":"cleanup","service_id":"old","epoch":1}}
        loop.transaction={"decision":self.choice,"status":"unknown","source_confirmed":True,"epoch":1,
                          "old":old,"deadline_ns":15,"uncertain_command":original}
        loop.work={"work_id":"first","release_ids":["old"],"install_ids":[],"queue":[]}
        loop.readout=loop.pending=None
        loop.native_plan_id="plan"
        loop.confirmed={"old":old}
        loop.retained=[old]
        loop.deferred_cleanups={}
        loop.defer_unreachable_cleanup=True
        loop.native_log=lambda row: None
        loop._event=lambda *args, **kwargs: None
        loop._defer_expired_cleanup(18)
        self.assertIsNotNone(loop.work)  # A nonterminal readback can also produce 'unknown'.
        loop.pending={"command":{"command_id":"q","target_node_id":"lost",
                                 "action":{"op":"query","service_id":"old","epoch":1}}}
        row={"event":"command_timeout","operation":"query","time_ns":17,"plan_id":"plan","command_id":"other-q"}
        loop._log(row)
        self.assertNotIn("cleanup_query_timeout",loop.transaction)
        loop._log(dict(row,command_id="q"))
        self.assertEqual(loop.transaction["cleanup_query_timeout"],("c","q"))
        loop.pending=None
        loop._defer_expired_cleanup(18)
        self.assertIsNone(loop.work)
        self.assertNotIn("cleanup_query_timeout",loop.deferred_cleanups["first"]["transaction"])

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
                          "epoch":1,"service_id":"old","plan_id":"plan","issued_at_ns":25,"command_id":"retry-write"})
        with self.assertRaisesRegex(ValueError,"before reconciliation"):
            self.validate()


if __name__=="__main__":
    unittest.main()
