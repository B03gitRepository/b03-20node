"""Public packet extension contract. This module uses only the Python standard library."""
from copy import deepcopy
from tempfile import SpooledTemporaryFile
import hashlib
import json

VERSION = "b03.packet-simulator.v2"
CHANNELS = ("event", "observation", "device", "meter")


def meter_record(row):
    return {"startNs": row["start_ns"], "endNs": row["end_ns"],
            "activeCount": len(row["active"]), "unmetCount": len(row["unmet"]),
            "unmetServiceNs": row["U_service_ns"], "weightedUnmetServiceNs": row["W_service_ns"]}


def execution_record(row):
    command = row["command"]
    return {"phase": "execution", "timeNs": row["time_ns"], "nodeId": row["node_id"],
            "commandId": command.get("command_id"), "operation": command.get("action", {}).get("op"),
            "status": row["status"], "executedAtNs": row["executed_at_ns"],
            "version": row["after_version"], "packetUid": row["packet_uid"]}


def ack_record(row):
    ack = row["ack"]
    return {"phase": "ackArrived", "timeNs": row["time_ns"], "nodeId": ack["node_id"],
            "commandId": ack["command_id"], "status": ack["status"], "executedAtNs": ack["executed_at_ns"],
            "version": ack["version"], "packetUid": row["packet_uid"], "readback": ack.get("b03_readback")}


def new_digests():
    return {name: hashlib.sha256() for name in ("execution", "ackArrived", "meter")}


def hash_record(accumulator, data):
    value = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    accumulator.update(value.encode("utf-8") + b"\n")


class Ports:
    """Dispatch detached records. Only the event port may request a local sensing action."""

    def __init__(self, provider=None):
        self.provider = provider
        self.counts = dict.fromkeys(CHANNELS, 0)
        self.requested = 0
        self.terminal = False
        self.after_drain_ns = None
        self._replay = None
        self._digests = new_digests()
        if provider is not None:
            for name in CHANNELS:
                if not callable(getattr(provider, "on_" + name, None)):
                    raise ValueError("Missing public simulator callback: on_" + name)

    def _deliver(self, channel, data):
        self.counts[channel] += 1
        if channel == "meter":
            hash_record(self._digests["meter"], data)
        elif channel == "device":
            hash_record(self._digests[data["phase"]], data)
        if self.provider is not None:
            method = getattr(self.provider, "on_" + channel)
            if method(deepcopy(data)) is not None:
                raise ValueError("Read-only simulator callbacks must return None: " + channel)

    def observe(self, channel, data):
        if self.terminal:
            raise RuntimeError("Public simulator callbacks are closed")
        if channel == "observation":
            self._deliver(channel, data)
            return
        if channel not in ("device", "meter"):
            raise ValueError("Unknown read-only simulator port")
        if self.provider is None:
            self._deliver(channel, data)
            return
        # Persist detached values in occurrence order, with bounded RAM even on a long simulation.
        if self._replay is None:
            self._replay = SpooledTemporaryFile(max_size=256 * 1024, mode="w+b")
        row = json.dumps([channel, data], separators=(",", ":"), allow_nan=False)
        self._replay.write(row.encode("utf-8") + b"\n")

    def finish(self, drain_ns):
        if self.terminal or type(drain_ns) is not int or drain_ns < 0:
            raise RuntimeError("Post-drain simulator callbacks require one finished episode")
        # Disable all action-bearing callbacks before exposing any evaluator or undelivered device truth.
        self.terminal = True
        try:
            if self._replay is not None:
                self._replay.seek(0)
                for line in self._replay:
                    channel, data = json.loads(line)
                    self._deliver(channel, data)
            self.after_drain_ns = drain_ns
        finally:
            self.close()

    def close(self):
        self.terminal = True
        if self._replay is not None:
            self._replay.close()
            self._replay = None

    def event(self, event):
        if self.terminal:
            raise RuntimeError("Action-bearing simulator port is closed")
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
        if self.after_drain_ns is None:
            raise RuntimeError("Post-drain delivery has not finished")
        return {"contractVersion": VERSION, "callbacks": dict(self.counts), "localProbesRequested": self.requested,
                "deviceAndMeterDeliveredAfterDrainNs": self.after_drain_ns,
                "recordSha256": {name: h.hexdigest() for name, h in self._digests.items()}}


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
    digests = new_digests()
    for item in controller:
        kind = item["event"]
        if kind in ("observation", "service_observation"):
            counts["observation"] += 1
        elif kind == "ack_received":
            counts["device"] += 1
            hash_record(digests["ackArrived"], ack_record(item))
    for item in executions:
        hash_record(digests["execution"], execution_record(item))
    for item in intervals:
        hash_record(digests["meter"], meter_record(item))
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
    if wanted:
        topology = json.loads((run / "input/net-topo.json").read_bytes())
        endpoints = {link["linkId"]: (link["srcNodeId"], link["dstNodeId"]) for link in topology["links"]}
    clock = summary["clock"]
    for row in rows(run / "batches.jsonl"):
        if not clock["start_ns"] <= row["time_ns"] < clock["end_ns"]:
            continue
        link_ids = [op["payload"]["attrs"]["linkId"] for op in row["operations"]
                    if op["kind"] == "official_event" and op["payload"]["eventType"] in ("link_state", "link_param_change")]
        if link_ids:
            counts["event"] += 1
            if wanted:
                events[(row["sequence"], row["time_ns"])] = {n for link in link_ids for n in endpoints[link]}
    if any(node not in events.get((seq, at), ()) for seq, at, node in wanted):
        raise ValueError("Probe was not requested at an actual changed endpoint")
    expected = {"contractVersion": VERSION, "callbacks": counts, "localProbesRequested": len(wanted),
                "deviceAndMeterDeliveredAfterDrainNs": clock["drain_end_ns"],
                "recordSha256": {name: h.hexdigest() for name, h in digests.items()}}
    numbers = receipt.get("callbacks") if isinstance(receipt, dict) else None
    if (not isinstance(numbers, dict) or any(type(x) is not int for x in numbers.values())
            or type(receipt.get("deviceAndMeterDeliveredAfterDrainNs")) is not int
            or type(receipt.get("localProbesRequested")) is not int or receipt != expected):
        raise ValueError("Public simulator callback counts, terminal clock or record digests differ from sealed native evidence")
