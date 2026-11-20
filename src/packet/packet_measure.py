"""Independent evaluation of installed device paths and original business seconds.

The actors never receive this object. Sampling is scheduled after native events at
the same nanosecond. All durations use the SimPy clock and a half-open window.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from resnet.control.policy import required_wire_bps
from resnet.simulation.simulation import Simulation


def request_row(spec):
    return {
        "serviceId": spec.service_id, "srcNodeId": spec.src_node_id, "dstNodeId": spec.dst_node_id,
        "priority": spec.priority, "serviceType": spec.service_type, "category": spec.category,
        "requirement": {"bandwidthBps": spec.required_bps, "delayMs": spec.delay_limit_s * 1000,
                        "jitterMs": spec.jitter_limit_s * 1000, "ber": spec.ber_limit,
                        "packetLossRate": spec.loss_limit},
    }


def installed_path(sim, sid):
    source, spec = sim.sources[sid], sim.services[sid]
    if not source.active or source.rate_bps <= 0 or not source.epoch:
        return None
    nodes, links = [spec.src_node_id], []
    while nodes[-1] != spec.dst_node_id:
        key = nodes[-1], sid, source.epoch
        if key in sim.retired or key not in sim.rules:
            return None
        peer, link = sim.rules[key]
        if peer in nodes or peer not in sim.links[link].endpoints or nodes[-1] not in sim.links[link].endpoints:
            return None
        nodes.append(peer)
        links.append(link)
    return {"nodes": nodes, "linkIds": links, "allocatedBps": source.rate_bps, "epoch": source.epoch}


class BusinessMeter:
    def __init__(self, sim, directory, solver):
        self.sim, self.solver = sim, solver
        self.file = (directory / "business-intervals.jsonl").open("x", encoding="utf-8")
        self.last = sim.start_ns
        self.previous = None
        self.u_ns = self.w_ns = self.request_ns = self.weighted_request_ns = 0
        self.by_sid = Counter()
        self.pair_valid_ns = self.endpoint_valid_ns = 0
        self.pair_fraction_ns = self.endpoint_fraction_ns = 0.0

    def view(self):
        sim = self.sim
        nodes = {n: {"_online": online} for n, online in sim.state.node_online.items()}
        links = {}
        for lid, native in sim.links.items():
            p = native.parameters
            a, b = native.endpoints
            links[lid] = {"linkId": lid, "srcNodeId": a, "dstNodeId": b, "_online": native.effective_up,
                          "params": {"bandwidthBps": p["capacity_bps"], "delayMs": p["delay_ms"],
                                     "jitterMs": p["jitter_ms"], "ber": p["ber"], "packetLossRate": p["packet_loss_rate"]}}
        requests = {sid: request_row(sim.services[sid]) for sid, demand in sim.demands.items() if demand["registered"] and demand["active"]}
        assignments = {sid: installed_path(sim, sid) for sid in requests}
        wires = Counter()
        for assignment in assignments.values():
            if assignment:
                wire = required_wire_bps(assignment["allocatedBps"], sim.config["payload_bytes"], 8)
                for lid in assignment["linkIds"]:
                    wires[lid] += wire
        assess = self.solver.assess(nodes, links, requests, assignments, sim.now / 1e9)
        overloaded = {lid for lid, load in wires.items() if load > links[lid]["params"]["bandwidthBps"]}
        reasons = {}
        for sid, row in assess["services"].items():
            faults = list(row["reasons"])
            if assignments[sid] and overloaded.intersection(assignments[sid]["linkIds"]):
                faults.append("PHYSICAL_WIRE_CAPACITY")
            if faults:
                reasons[sid] = faults
        return {
            "active": {sid: req["priority"] for sid, req in requests.items()}, "unmet": reasons,
            "installed": {sid: route for sid, route in assignments.items() if route},
            "activeWireBps": dict(sorted(wires.items())), "wireOverload": sorted(overloaded),
            "onlineNodeCount": assess["onlineNodeCount"], "onlinePairDenominator": assess["onlineNodePairDenominator"],
            "connectedPairFraction": assess["onlineNodePairConnectedFraction"],
            "activeEndpointDenominator": assess["originalActiveEndpointDenominator"],
            "activeEndpointReachableFraction": assess["originalActiveEndpointReachableFraction"],
        }

    def interval(self, end):
        x = self.previous
        dt = end - self.last
        if x is None or dt <= 0:
            return
        loss = x["unmet"]
        weights = x["active"]
        delta_u = dt * len(loss)
        delta_w = dt * sum(weights[s] for s in loss)
        self.u_ns += delta_u
        self.w_ns += delta_w
        self.request_ns += dt * len(weights)
        self.weighted_request_ns += dt * sum(weights.values())
        for sid in loss:
            self.by_sid[sid] += dt
        if x["connectedPairFraction"] is not None:
            self.pair_fraction_ns += x["connectedPairFraction"] * dt
            self.pair_valid_ns += dt
        if x["activeEndpointReachableFraction"] is not None:
            self.endpoint_fraction_ns += x["activeEndpointReachableFraction"] * dt
            self.endpoint_valid_ns += dt
        value = {"start_ns": self.last, "end_ns": end, "U_service_ns": delta_u, "W_service_ns": delta_w, **x}
        self.file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def sample(self):
        now = self.sim.now
        if now < self.sim.start_ns or now > self.sim.end_ns:
            return
        if now == self.sim.end_ns:
            self.interval(now)
            self.last = now
            self.previous = None
            return
        view = self.view()
        if view != self.previous:
            self.interval(now)
            self.last, self.previous = now, view

    def close(self):
        self.file.close()
        return {"U": self.u_ns / 1e9, "W": self.w_ns / 1e9, "requestSeconds": self.request_ns / 1e9,
                "priorityRequestSeconds": self.weighted_request_ns / 1e9,
                "UOverRequests": self.u_ns / self.request_ns if self.request_ns else None,
                "onlinePairConnectedTimeMean": self.pair_fraction_ns / self.pair_valid_ns if self.pair_valid_ns else None,
                "activeEndpointReachableTimeMean": self.endpoint_fraction_ns / self.endpoint_valid_ns if self.endpoint_valid_ns else None,
                "unmetSecondsByService": {k: v / 1e9 for k, v in sorted(self.by_sid.items())}}


class MeasuredSimulation(Simulation):
    """Observe native transitions. A named receive fault is optional in a separate run."""
    def __init__(self, *args, drop_install_ack=False, **kwargs):
        self.meter = None
        self._meter_at = None
        self.drop_install_ack = drop_install_ack
        self.dropped_ack = None
        super().__init__(*args, **kwargs)

    def start_meter(self, directory, solver):
        self.meter = BusinessMeter(self, directory, solver)
        self.schedule(self.start_ns, self.meter.sample, priority=10)
        self.schedule(self.end_ns, self.meter.sample, priority=10)

    def log(self, name, row):
        super().log(name, row)
        if self.meter is not None and name in {"batches", "executions", "source-state", "service-local", "routes"}:
            if self.start_ns <= self.now < self.end_ns and self._meter_at != self.now:
                self._meter_at = self.now
                self.schedule(self.now, self.meter.sample, priority=10)

    def _link_arrival(self, packet, peer_node_id, link_id):
        msg = packet.message if packet.kind == "control" else None
        if (self.drop_install_ack and self.dropped_ack is None and msg and msg.get("kind") == "ack"
                and msg.get("status") == "applied" and msg.get("reason") == "install"
                and peer_node_id == packet.dst_node_id and packet.src_node_id != peer_node_id):
            self.dropped_ack = {"event": "withhold_received_ack", "time_ns": self.now, "packet_uid": packet.uid,
                                "node": packet.src_node_id, "last_link": link_id, "command_id": msg["command_id"],
                                "plan_id": msg["plan_id"], "device_effect_ns": msg["executed_at_ns"]}
            self.log("fault", self.dropped_ack)
            self._drop(packet, "b03_receiver_fault_single_ack", link_id)
            return
        super()._link_arrival(packet, peer_node_id, link_id)
