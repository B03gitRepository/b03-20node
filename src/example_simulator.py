"""Native-compatible packet provider with device-local link-change notifications."""
from __future__ import annotations

from functools import partial


def build(settings):
    """Return a packet backend that samples changed ports after local detection latency."""
    from resnet.simulation.simulation import Simulation

    if set(settings) != {"detectionDelayNs"}:
        raise ValueError("The event-probe simulator requires detectionDelayNs")
    delay = settings["detectionDelayNs"]
    if type(delay) is not int or delay <= 0:
        raise ValueError("detectionDelayNs must be a positive integer")

    class EventProbeSimulation(Simulation):
        adapter_id = "example.local-port-probes"
        adapter_version = "1.0.0"
        contract_version = "b03.packet-simulator.v1"

        def _apply_batch(self, batch):
            super()._apply_batch(batch)
            if self.control is None or not self.start_ns <= self.now < self.end_ns:
                return
            at = self.now + delay
            if at >= self.end_ns:
                return
            changed = {}
            for operation in batch.operations:
                if operation["kind"] != "official_event":
                    continue
                event = operation["payload"]
                if event["eventType"] not in ("link_param_change", "link_state"):
                    continue
                link = event["attrs"]["linkId"]
                for node in self.links[link].endpoints:
                    changed.setdefault(node, set()).add(link)
            for node, links in sorted(changed.items()):
                self.schedule(at, partial(self._sample_local_ports, node, sorted(links), self.now))

        def _sample_local_ports(self, node, links, changed_at):
            if self.state.node_online[node]:
                self.log("simulator-events", {"event": "device_port_detection", "node": node, "links": links,
                                             "changed_at_ns": changed_at, "time_ns": self.now})
                self.control.send_probes(node)

    return EventProbeSimulation
