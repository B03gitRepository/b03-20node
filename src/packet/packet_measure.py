"""Independent evaluation of installed device paths and original business seconds.

The actors never receive this object. Sampling is scheduled after native events at
the same nanosecond. All durations use the SimPy clock and a half-open window.
"""
from __future__ import annotations

import b03_formulas as formula
import json
from collections import Counter
from pathlib import Path


def request_row(spec):
    return {
        "serviceId": spec.service_id, "srcNodeId": spec.src_node_id, "dstNodeId": spec.dst_node_id,
        "priority": spec.priority, "serviceType": spec.service_type, "category": spec.category,
        "requirement": {"bandwidthBps": spec.required_bps, "delayMs": formula.seconds_to_ms(spec.delay_limit_s),
                        "jitterMs": formula.seconds_to_ms(spec.jitter_limit_s), "ber": spec.ber_limit,
                        "packetLossRate": spec.loss_limit},
    }


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
        nodes, links, requests, assignments = sim.meter_snapshot()
        wires = Counter()
        for assignment in assignments.values():
            if assignment:
                wire = sim.payload_wire_bps(assignment["allocatedBps"])
                for lid in assignment["linkIds"]:
                    wires[lid] += wire
        assess = self.solver.assess(nodes, links, requests, assignments, formula.ns_to_seconds(sim.now))
        overloaded = {lid for lid, load in wires.items() if not formula.within_capacity(load, links[lid]["params"]["bandwidthBps"])}
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
        delta_u, delta_w = formula.service_losses(dt, loss, weights)
        self.u_ns += delta_u
        self.w_ns += delta_w
        self.request_ns += formula.service_ns(dt, len(weights))
        self.weighted_request_ns += formula.service_ns(dt, sum(weights.values()))
        for sid in loss:
            self.by_sid[sid] += dt
        pair, pair_dt = formula.time_fraction(dt, x["connectedPairFraction"])
        endpoint, endpoint_dt = formula.time_fraction(dt, x["activeEndpointReachableFraction"])
        self.pair_fraction_ns += pair
        self.pair_valid_ns += pair_dt
        self.endpoint_fraction_ns += endpoint
        self.endpoint_valid_ns += endpoint_dt
        value = {"start_ns": self.last, "end_ns": end, "U_service_ns": delta_u, "W_service_ns": delta_w, **x}
        self.sim.meter_interval(value)
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
        return {"U": formula.ns_to_seconds(self.u_ns), "W": formula.ns_to_seconds(self.w_ns), "requestSeconds": formula.ns_to_seconds(self.request_ns),
                "priorityRequestSeconds": formula.ns_to_seconds(self.weighted_request_ns),
                "UOverRequests": formula.ratio(self.u_ns,self.request_ns),
                "onlinePairConnectedTimeMean": formula.ratio(self.pair_fraction_ns,self.pair_valid_ns),
                "activeEndpointReachableTimeMean": formula.ratio(self.endpoint_fraction_ns,self.endpoint_valid_ns),
                "unmetSecondsByService": {k: formula.ns_to_seconds(v) for k, v in sorted(self.by_sid.items())}}
