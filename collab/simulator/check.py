"""Run a partner's simulator callbacks without the packet engine or official data."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from simulator_ports import Ports, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", type=Path, default=ROOT / "src/example_public_simulator.py")
    parser.add_argument("--factory", default="build")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).with_name("case.json"))
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("partner_simulator", args.provider)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sample = json.loads(args.fixture.read_bytes())
    provider = getattr(module, args.factory)(sample["settings"])
    if provider.contractVersion != VERSION or not provider.id or not provider.version:
        raise ValueError("Missing simulator identity")
    ports = Ports(provider)
    requests = ports.event(sample["event"])
    if args.provider.resolve() == (ROOT / "src/example_public_simulator.py").resolve():
        expect = [(item["nodeId"], item["delayNs"]) for item in sample["expectedRequests"]]
        if requests != expect:
            raise ValueError("Unexpected local probe requests")
    ports.observe("observation", sample["observation"])
    for row in sample["device"]:
        ports.observe("device", row)
    ports.observe("meter", sample["meter"])
    print(json.dumps(ports.receipt(), sort_keys=True))


if __name__ == "__main__":
    main()
