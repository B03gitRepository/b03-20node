"""Source-only regressions. Native end-to-end runs are documented in README.md."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"src/rules/vendor/src")]
from opening import check_opening
from scene import PROFILE, signed_inputs
from frozen_policy import indexed_table


class ReleaseContracts(unittest.TestCase):
    def test_build_contains_all_original_script_pins(self):
        self.assertEqual({k for k in PROFILE["pins"] if k.startswith("scripts/")},
                         {f"scripts/script-{i:02}.json" for i in range(1,11)})
        self.assertEqual(len(PROFILE["pins"]),12)
        with self.assertRaises(ValueError):
            signed_inputs({"net-topo.json":b"x","bg-service.json":b"x","scripts/script-99.json":b"[]"},"99")
        with self.assertRaises(ValueError):
            signed_inputs({"net-topo.json":b"x","bg-service.json":b"x","scripts/script-01.json":b"[]"},"01")

    def test_residual_search_freezes_existing_path_and_permissions(self):
        nodes={k:{"_online":True} for k in ("a","b","c")}
        def link(lid,a,b,cap):
            return {"linkId":lid,"srcNodeId":a,"dstNodeId":b,"_online":True,
                    "params":{"bandwidthBps":cap,"delayMs":1,"jitterMs":0,"ber":0,"packetLossRate":0}}
        links={z["linkId"]:z for z in (link("ab","a","b",1000),link("ac","a","c",5000),link("cb","c","b",5000))}
        def req(sid,bps):
            return {"serviceId":sid,"srcNodeId":"a","dstNodeId":"b","priority":4,
                    "requirement":{"bandwidthBps":bps,"delayMs":10,"jitterMs":5,"ber":1e-5,"packetLossRate":0.01}}
        snapshot={"nodes":nodes,"links":links,"requests":{k:req(k,v) for k,v in [("healthy",800),("open",2400),("protected",200)]},"observedAtSec":5.0}
        old={"healthy":{"nodes":["a","b"],"linkIds":["ab"],"allocatedBps":800},"open":None,"protected":None}
        conf={"path_limit":4,"path_expansion_budget":100,"max_path_hops":None}
        authority={"executableServiceIds":["open"]}
        proposed,log=check_opening(old,snapshot,conf,authority,"residual",False)
        self.assertEqual(proposed["healthy"],old["healthy"])
        self.assertIsNone(old["open"]);self.assertIsNone(proposed["protected"])
        self.assertEqual((proposed["open"]["linkIds"],proposed["open"]["allocatedBps"]),(["ac","cb"],2400))
        self.assertEqual(log["applied"],["open"])
        for mode,hold in [("audit",False),("residual",True)]:
            kept,record=check_opening(old,snapshot,conf,authority,mode,hold)
            self.assertEqual(kept,old);self.assertEqual(record["applied"],[])
            self.assertEqual(record["feasibleAdditions"],["open"])

    def test_model_table_roundtrips_missing_and_numeric_values(self):
        raw=[[0,True,None,1],[1,False,4,1],[2,True,5,2]]
        packet=indexed_table(["id","fresh","capacity","priority"],raw)
        decoded=[]
        for identifier,mask,*items in packet["rows"]:
            values=iter(items)
            decoded.append([identifier,*[next(values) if mask&(1<<i) else v for i,v in enumerate(packet["defaults"])]])
        self.assertEqual(decoded,raw)


if __name__=="__main__":
    unittest.main()
