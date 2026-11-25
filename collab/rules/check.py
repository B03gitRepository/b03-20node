"""Exercise a rule provider using only Python's standard library and the synthetic case."""
import argparse
from copy import deepcopy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "src"
sys.path.insert(0, str(SOURCE))
import b03_formulas as formula

DEFAULT = SOURCE / "collab_patch_rule.py"


def load(path, factory):
    spec = importlib.util.spec_from_file_location("team_rule", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, factory)


def check(msg, reply):
    if not isinstance(reply, dict) or not {"patch"} <= reply.keys() <= {"patch","audit"}:
        raise ValueError("Expected a patch and optional audit")
    json.dumps(reply, allow_nan=False)
    snapshot = msg["snapshot"]
    patch = reply["patch"]
    grant = msg["authority"]
    scope = set(msg["action"]["scope"]["serviceIds"]) & set(grant["executableServiceIds"])
    scope -= set(grant["frozenHealthyServiceIds"]) | set(grant["protectedServiceIds"])
    if not isinstance(patch, dict) or set(patch) - scope:
        raise ValueError("Patch outside scope")
    if sum(p!=snapshot["assignments"][s] for s,p in patch.items()) > msg["budgets"]["maxChangedServices"]:
        raise ValueError("Too many changed services")
    load_bps = {}
    for sid, path in {**snapshot["assignments"], **patch}.items():
        if path is None:
            continue
        service = snapshot["requests"][sid]
        req = service["requirement"]
        nodes, edges = path["nodes"],path["linkIds"]
        if (set(path)!={"nodes","linkIds","allocatedBps"} or path["allocatedBps"]!=req["bandwidthBps"]
                or not edges or len(nodes)!=len(edges)+1 or len(set(nodes))!=len(nodes)
                or nodes[0]!=service["srcNodeId"] or nodes[-1]!=service["dstNodeId"]
                or sid in patch and len(edges)>msg["budgets"]["maxHops"]):
            raise ValueError("Invalid route or service rate")
        quality = []
        for a,b,lid in zip(nodes,nodes[1:],edges):
            link = snapshot["links"][lid]
            if ({a,b}!={link["srcNodeId"],link["dstNodeId"]} or not link["_online"]
                    or not all(snapshot["nodes"][n]["_online"] for n in (a,b))):
                raise ValueError("Route continuity or availability failed")
            load_bps[lid] = load_bps.get(lid,0) + path["allocatedBps"]
            quality.append(link["params"])
        quality_metrics = formula.path_metrics(quality)
        if any(quality_metrics[k] > req[k] for k in ("delayMs", "jitterMs", "ber", "packetLossRate")):
            raise ValueError("Original QoS violated")
    if any(not formula.within_capacity(v, snapshot["links"][k]["params"]["bandwidthBps"]) for k,v in load_bps.items()):
        raise ValueError("Link reservation exceeded")
    audit = reply.get("audit",{})
    if not isinstance(audit,dict) or set(audit.get("usedKnowledge",[]))-{r["id"] for r in msg.get("knowledgeContext",[])}:
        raise ValueError("Audit references undelivered knowledge")


def export_kit(directory):
    """Copy the minimal, self-contained standard-library runner into a new directory."""
    root = HERE.parents[1]
    members = ("collab/rules/check.py", "collab/rules/case.json", "src/collab_patch_rule.py",
               formula.IMPLEMENTATION, formula.DECLARATION)
    directory.mkdir(parents=True, exist_ok=False)
    for name in members:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    return members


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", type=Path,default=DEFAULT)
    ap.add_argument("--factory",default="build_rules")
    ap.add_argument("--registration", type=Path, help="Resolved rule registration JSON for a different provider")
    ap.add_argument("--case",type=Path,default=HERE/"case.json")
    ap.add_argument("--export-kit", type=Path, help="Export the standalone kit into a new directory")
    opts = ap.parse_args()
    if opts.export_kit is not None:
        print(json.dumps({"files": export_kit(opts.export_kit)}))
        return
    case = json.loads(opts.case.read_bytes())
    registered = json.loads(opts.registration.read_bytes()) if opts.registration else case["registration"]
    provider = load(opts.provider,opts.factory)(config=deepcopy(registered))
    msg = case["solve"]
    offer = {k:deepcopy(v) for k,v in msg.items() if k not in ("action","knowledgeContext")}
    offer.update(deepcopy(case["enumerate"]))
    ids = provider.enumerate_actions(offer)
    if (not isinstance(ids,list) or len(set(ids))!=len(ids)
            or set(ids)-{r["id"] for r in offer["candidates"]} or msg["action"]["id"] not in ids):
        raise ValueError("Fixture action unavailable or illegal")
    output = provider.solve_action(deepcopy(msg))
    check(msg, output)
    if opts.provider.resolve()==DEFAULT.resolve() and opts.case.resolve()==(HERE/"case.json"):
        if output!=case["output"]:
            raise ValueError("Reference fixture changed")
        swapped = deepcopy(msg)
        swapped["knowledgeContext"].reverse()
        alt = provider.solve_action(swapped)
        check(swapped,alt)
        if alt["patch"]==output["patch"] or alt["audit"]["orderBy"]!="hops":
            raise ValueError("Reference did not consume the alternate ranking")
    print(json.dumps({"legalTools":ids,"result":output},sort_keys=True))


if __name__ == "__main__":
    main()
