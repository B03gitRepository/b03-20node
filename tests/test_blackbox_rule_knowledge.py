"""Synthetic packet work orders for the ranked knowledge and rule patch contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("src","src/rules","src/rules/vendor/src","src/packet","simulator/resnet_py/src")]
from component_config import load_components
from frozen_policy import DecisionPolicy
from knowledge_ports import accepts_model, verify
from packet_roles import _PacketAgents, digest
from rule_component import verify_patch_receipt
from tech_knowledge import query, _identity, _entries


def synthetic_state():
    example = json.loads((ROOT/"collab/rules/case.json").read_bytes())["solve"]["snapshot"]
    raw_links = {}
    for key, link in example["links"].items():
        props = link["params"]
        raw_links[key] = {"src_node_id":link["srcNodeId"],"dst_node_id":link["dstNodeId"],"usable":True,
                          "reason":"reported_up","capacity_bps":props["bandwidthBps"],"delay_ns":int(props["delayMs"]*1e6),
                          "jitter_ns":int(props["jitterMs"]*1e6),"ber":props["ber"],"packet_loss_rate":props["packetLossRate"]}
    demand = {"service_id":"ribbon","src_node_id":"aster","dst_node_id":"cedar","priority":4,
              "category":"demo","service_type":"stream","required_bps":1234,"delay_limit_s":.015,
              "jitter_limit_s":.003,"ber_limit":.00001,"loss_limit":.002}
    row = {"service":demand,"registered":True,"active":True,"fresh":True,"revision":2,"observed_at_ns":12,
           "received_at_ns":18,"seq":2,"node_id":"aster"}
    return {"now_ns":55,"links":raw_links,"node_versions":{},"services":{"ribbon":row},"confirmed":{},
            "retained_wire_bps":{},"link_ttl_ns":1000}


def scenario(*, scope=None, budget=None, method="X", ports=None, hold_patch=False, delayed=False, state=None, response=None):
    c = load_components(ROOT/"config/collab-rules-knowledge.json").activate(method)
    reg = c.rules["options"]["customTools"]["X"]
    if scope:
        reg["scope"] = scope
    if budget:
        reg["budget"] = "case"
        c.rules["options"]["outputBudgets"] = {"case":budget}
    if ports is not None:
        c.knowledge["bindings"] = {k:{"topic":"detour guidance"} for k in ports}
    chooser = DecisionPolicy(method, components=c)
    state = deepcopy(state) if state is not None else synthetic_state()
    roster = sorted(state["services"])
    agents = _PacketAgents(roster,[],0,chooser)
    snapshot = digest(state)
    wid = "example-order"
    captured = {}
    def exchange(op, tag, snap, payload, parent_message_id=None):
        captured["diagnosis"] = deepcopy(payload["diagnosis"])
        return agents.handle({"kind":op,"work_id":tag,"snapshot_id":snap,"payload":payload,
                              "parent_message_id":parent_message_id}), {}
    agents.exchange = exchange
    def empty(self, request):
        if delayed:
            import time
            time.sleep(.005)
        return deepcopy(response) if response is not None else {"patch":{},"audit":{"expanded":0}}
    def run():
        return agents.handle({"kind":"observe","work_id":wid,"snapshot_id":snapshot,"message_id":"stage",
                              "payload":{"state":state,"policy_hash":agents.policy_hash}})
    if hold_patch or response is not None:
        with patch("collab_patch_rule.Router.solve_action", empty):
            plan = run()
    else:
        plan = run()
    dx=plan.pop("diagnosis")
    plan["time_ns"]=state["now_ns"]
    diagnosis={"event":"b03_om_diagnosis","time_ns":state["now_ns"],"work_id":wid,"snapshot_id":snapshot,**dx}
    plan["event"]="b03_rc_plan"
    evidence={"event":"b03_observation_evidence","purpose":"plan","time_ns":state["now_ns"],
              "work_id":wid,"snapshot_id":snapshot,"state":state}
    cond={"components":c.identity,"rcPolicy":chooser.identity,
          "simulation":{"control":{"control_headroom_fraction":0}},
          "authority":{"discovery":"explicit_roster","allowedServiceIds":roster,"protectedServiceIds":[]}}
    return c,cond,plan,diagnosis,evidence,captured


def replay(c, cond,plan,diagnosis,evidence, captured):
    library=json.loads(c.knowledge_path.read_bytes())
    entries=_entries(library,c.knowledge["version"])
    return verify(cond,[evidence,diagnosis,plan],entries,_identity(c.identity))


class Contracts(unittest.TestCase):
    def test_ranking_and_exact_ids(self):
        c=load_components(ROOT/"config/collab-rules-knowledge.json").activate("knowledge")
        self.assertEqual([v["id"] for v in query(c,topic="detour guidance")],["EX.PATH.DELAY","EX.PATH.HOPS"])
        self.assertEqual(query(c,topic="unrelated test word"),[])
        self.assertEqual(query(c,identifier="nonexistent"),[])
        for invalid in (["EX.PATH.DELAY","EX.PATH.DELAY"],["invented"]):
            with patch.object(c._ranked_knowledge[2],"lookup",return_value=invalid),self.assertRaises(ValueError):
                query(c,topic="detour guidance")
        with patch.object(c._ranked_knowledge[2],"lookup",return_value=["EX.PATH.HOPS"]),self.assertRaises(ValueError):
            query(c,identifier="EX.PATH.DELAY")

    def test_delivered_advice_and_om_authority(self):
        result = scenario(ports=["om","rules","model"])
        c,conditions,plan,diagnosis,evidence,capture=result
        self.assertEqual(set(plan["knowledge_contexts"]),{"om","rules"})
        self.assertEqual(capture["diagnosis"]["knowledgeContext"],diagnosis["knowledgeContext"])
        self.assertEqual(replay(*result)[0]["port"],"om")
        base=scenario(ports=[])
        self.assertEqual(base[3]["authority"],diagnosis["authority"])
        self.assertTrue(plan["decisions"]["ribbon"]["accepted"])
        stock=scenario(method="I",ports=["rules","model"])
        self.assertNotIn("knowledge_contexts",stock[2])
        self.assertEqual(replay(*stock),[])
        frozen={"entrypoint":"builtin","options":{"acceptKnowledge":True}}
        self.assertFalse(accepts_model(frozen,{"kind":"frozen-model"}))

    def test_selected_scope_and_limits_bind_residual_opening(self):
        for kwargs in ({"scope":"retain"},{"budget":{"maxChangedServices":0}},{"budget":{"maxHops":0}}):
            result=scenario(hold_patch=True,**kwargs)
            self.assertEqual(result[2]["install_ids"],[])
            self.assertFalse(result[2]["decisions"]["ribbon"]["accepted"])
            replay(*result)
        normal=scenario(hold_patch=True)
        self.assertEqual(normal[2]["install_ids"],["ribbon"])
        replay(*normal)
        onehop=scenario(hold_patch=True,budget={"maxHops":1})
        self.assertEqual(onehop[2]["decisions"]["ribbon"]["link_path"],["rose"])
        replay(*onehop)

    def test_new_revision_keeps_unmentioned_epoch_and_charge(self):
        state = synthetic_state()
        installed = deepcopy(scenario()[2]["decisions"]["ribbon"])
        installed.update(demand_revision=1, revision=1)
        state["confirmed"]["ribbon"] = {"decision":installed,"epoch":1,"confirmed_at_ns":20,
                "configuration_evidence":{"verified":True,"source_mode":"revoked_permit_old_rules"}}
        charge = {lid:installed["wire_bps"] for lid in installed["link_path"]}
        state["retained_wire_bps"] = dict(charge)
        for kwargs in ({"scope":"retain"},{"budget":{"maxChangedServices":0}},{}):
            result = scenario(state=state,hold_patch=True,**kwargs)
            plan = result[2]
            self.assertFalse(plan["decisions"]["ribbon"]["accepted"])
            self.assertEqual(plan["decisions"]["ribbon"]["demand_revision"],2)
            self.assertEqual((plan["release_ids"],plan["install_ids"],plan["scope"]["changedServiceIds"]),([],[],[]))
            self.assertEqual(plan["wire_ledger"]["proposed_wire_bps"],charge)
            replay(*result)
            plan["release_ids"] = ["ribbon"]
            plan["scope"]["changedServiceIds"] = ["ribbon"]
            with self.assertRaisesRegex(ValueError,"release or installation scope"):
                replay(*result)
        replaced = scenario(state=state,budget={"maxChangedServices":1})
        self.assertEqual(replaced[2]["scope"]["changedServiceIds"],["ribbon"])
        self.assertEqual((replaced[2]["release_ids"],replaced[2]["install_ids"]),(["ribbon"],["ribbon"]))
        self.assertTrue(replaced[2]["decisions"]["ribbon"]["accepted"])
        replay(*replaced)
        with self.assertRaisesRegex(ValueError,"Final device operations exceed"):
            scenario(state=state,budget={"maxChangedServices":0},response={"patch":{"ribbon":None}})
        cleared = scenario(state=state,budget={"maxChangedServices":1},response={"patch":{"ribbon":None}})
        self.assertEqual((cleared[2]["release_ids"],cleared[2]["install_ids"]),(["ribbon"],[]))
        replay(*cleared)
        self.assertEqual(cleared[2]["wire_ledger"]["proposed_wire_bps"],{})

    def test_no_other_service_can_spend_an_unreleased_old_epoch(self):
        state = synthetic_state()
        installed = deepcopy(scenario()[2]["decisions"]["ribbon"])
        installed.update(demand_revision=1, revision=1)
        state["confirmed"]["ribbon"] = {"decision":installed,"epoch":1,"confirmed_at_ns":20,
                                       "configuration_evidence":{"verified":True,"source_mode":"revoked_permit_old_rules"}}
        charge = {lid:installed["wire_bps"] for lid in installed["link_path"]}
        state["retained_wire_bps"] = dict(charge)
        for lid,row in state["links"].items():
            row["capacity_bps"] = charge.get(lid, 20)
        other = deepcopy(state["services"]["ribbon"])
        other["service"]["service_id"] = "lantern"
        state["services"]["lantern"] = other
        route = {"nodes":installed["node_path"],"linkIds":installed["link_path"],"allocatedBps":installed["wire_bps"]}
        with self.assertRaisesRegex(ValueError, "physical wire budget"):
            scenario(state=state, response={"patch":{"lantern":route}})
        held = scenario(state=state,hold_patch=True)
        self.assertFalse(held[2]["install_ids"])
        self.assertEqual(held[2]["wire_ledger"]["proposed_wire_bps"],charge)
        replay(*held)

    def test_actual_cost_cannot_be_replaced_by_self_report(self):
        with self.assertRaisesRegex(ValueError,"measured work-order deadline"):
            scenario(budget={"maxSolverWallMs":1},hold_patch=True,delayed=True)

    def test_replay_detects_related_hash_rewrites_and_suppressed_plan(self):
        for change in ("snapshot","budget","silent_rejection","receipt_rank","scope"):
            result=scenario()
            _,_,plan,diagnosis,_,_=result
            audit=plan["solver_audit"]
            req=audit["rulePatch"]["input"]
            if change=="snapshot":
                req["snapshot"]["requests"]["ribbon"]["requirement"]["delayMs"]+=20
                audit["binding"]["physicalStateHash"]=digest(req["snapshot"])
            elif change=="budget":
                audit["rulePatch"]["hostConfig"]["path_limit"]+=20
                req["budgets"]["path_limit"]+=20
                audit["binding"]["configSha256"]=digest(audit["rulePatch"]["hostConfig"])
            elif change=="silent_rejection":
                plan["decisions"]["ribbon"]["accepted"]=False
                plan["install_ids"]=[]
            elif change=="receipt_rank":
                plan["knowledge_contexts"]["rules"]["output"].reverse()
            else:
                req["authority"]["executableServiceIds"].clear()
                diagnosis["authority"]["executableServiceIds"].clear()
                audit["binding"]["authorityHash"]=digest(req["authority"])
            with self.subTest(change=change), self.assertRaises(ValueError):
                replay(*result)


if __name__=="__main__":
    unittest.main()
