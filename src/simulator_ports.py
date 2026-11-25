"""Public packet extension contract. This module uses only the Python standard library."""
from copy import deepcopy
import json

VERSION = "b03.packet-simulator.v2"
CHANNELS = ("event", "observation", "device", "meter")


class Ports:
    """Dispatch detached records. Only the event port may request a local sensing action."""

    def __init__(self, provider=None):
        self.provider = provider
        self.counts = dict.fromkeys(CHANNELS, 0)
        self.requested = 0
        if provider is not None:
            for name in CHANNELS:
                if not callable(getattr(provider, "on_" + name, None)):
                    raise ValueError("Missing public simulator callback: on_" + name)

    def observe(self, channel, data):
        if channel not in ("observation", "device", "meter"):
            raise ValueError("Unknown read-only simulator port")
        self.counts[channel] += 1
        if self.provider is not None:
            method = getattr(self.provider, "on_" + channel)
            if method(deepcopy(data)) is not None:
                raise ValueError("Read-only simulator callbacks must return None: " + channel)

    def event(self, event):
        self.counts["event"] += 1
        if self.provider is None:
            return []
        requests = self.provider.on_event(deepcopy(event))
        allowed = set(event["probeNodes"])
        if type(requests) is not list or len(requests) > len(allowed):
            raise ValueError("Expected at most one local probe per changed endpoint")
        seen = set()
        result = []
        for item in requests:
            if not isinstance(item, dict) or set(item) != {"op", "nodeId", "delayNs"}:
                raise ValueError("Expected a local probe request")
            node, delay = item["nodeId"], item["delayNs"]
            if (item["op"] != "probe" or not isinstance(node, str) or node not in allowed or node in seen
                    or type(delay) is not int or not 0 < delay < event["remainingNs"]):
                raise ValueError("Local probe outside its node, time or action scope")
            seen.add(node)
            result.append((node, delay))
        self.requested += len(result)
        return result

    def receipt(self):
        return {"contractVersion": VERSION, "callbacks": dict(self.counts), "localProbesRequested": self.requested}


def rows(path):
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def verify_ports(run, conditions, summary, controller, executions, intervals):
    registration = (conditions.get("components") or {}).get("simulator") or {}
    receipt = summary.get("simulatorPorts")
    if registration.get("contractVersion") != VERSION:
        if receipt is not None:
            raise ValueError("Unexpected public simulator receipt for a legacy provider")
        return
    members = json.loads((run / "RUN_FILES.json").read_bytes())["files"]
    for name in ("batches.jsonl", "controller.jsonl", "executions.jsonl", "business-intervals.jsonl",
                 "simulator-events.jsonl"):
        if (run / name).is_file() and name not in members:
            raise ValueError("Unsealed public simulator evidence: " + name)
    counts = dict.fromkeys(CHANNELS, 0)
    for item in controller:
        kind = item["event"]
        if kind in ("observation", "service_observation"):
            counts["observation"] += 1
        elif kind == "ack_received":
            counts["device"] += 1
    counts["device"] += len(executions)
    counts["meter"] = len(intervals)
    trace = list(rows(run / "simulator-events.jsonl"))
    wanted = {}
    resolved = set()
    for item in trace:
        key = (item["sequence"], item["changedAtNs"], item["nodeId"])
        if item["event"] == "probe_requested":
            if key in wanted or not summary["clock"]["start_ns"] <= key[1] < item["dueNs"] < summary["clock"]["end_ns"]:
                raise ValueError("Duplicate or out-of-window public probe")
            wanted[key] = item["dueNs"]
        elif item["event"] in ("probe_sampled", "probe_skipped_offline"):
            if key in resolved or wanted.get(key) != item["timeNs"]:
                raise ValueError("Unexpected local probe callback")
            resolved.add(key)
        else:
            raise ValueError("Unknown public simulation trace event")
    if set(wanted) != resolved:
        raise ValueError("Unresolved scheduled local probe")
    events = {}
    batches = run / "batches.jsonl"
    if wanted:
        topology = json.loads((run / "input/net-topo.json").read_bytes())
        endpoints = {link["linkId"]: (link["srcNodeId"], link["dstNodeId"]) for link in topology["links"]}
        for row in rows(batches):
            counts["event"] += 1
            allowed = set()
            for operation in row["operations"]:
                data = operation.get("payload", {})
                if operation["kind"] == "official_event" and data["eventType"] in ("link_state", "link_param_change"):
                    allowed.update(endpoints[data["attrs"]["linkId"]])
            events[(row["sequence"], row["time_ns"])] = allowed
    elif batches.is_file():
        # The archive verifier has already authenticated every byte; only the number of records is needed here.
        with batches.open("rb") as stream:
            counts["event"] = sum(1 for line in stream if line.strip())
    if any(node not in events.get((seq, at), ()) for seq, at, node in wanted):
        raise ValueError("Probe was not requested at an actual changed endpoint")
    expected = {"contractVersion": VERSION, "callbacks": counts, "localProbesRequested": len(wanted)}
    numbers = receipt.get("callbacks") if isinstance(receipt, dict) else None
    if (not isinstance(numbers, dict) or any(type(x) is not int for x in numbers.values())
            or type(receipt.get("localProbesRequested")) is not int or receipt != expected):
        raise ValueError("Public simulator callback counts differ from sealed native evidence")
