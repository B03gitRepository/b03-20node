"""Map the native resnet implementation onto the B03 clock, observation, device and meter ports."""
from collections import Counter
from copy import deepcopy
from functools import partial
from types import SimpleNamespace

from resnet.control.diagnosis import is_active_demand
from resnet.control.interfaces import validate_policy_result
from resnet.control.loop import ControlLoop
from resnet.control.policy import _quality
from resnet.control.policy import required_wire_bps
from resnet.simulation.simulation import Simulation

from packet_measure import BusinessMeter, request_row
from packet_device_readback import rule_fields


def validate_install_choice(choice, service, links, now, qos_profile):
    """Validate a full native payload and original five QoS limits at the final controller gate."""
    validate_policy_result({"decided_at_ns": now, "decisions": [choice]}, service, links, now_ns=now)
    if not choice["accepted"] or choice["reserved_bps"] != service.required_bps:
        raise ValueError("original_full_payload_rate_required")
    if qos_profile == "official":
        _, violations = _quality(service, choice["link_path"], links["links"], 1036, qos_profile="official")
        if violations:
            raise ValueError("original_qos:" + ",".join(violations))


class ReportingExecutor:
    """Delegate all commands to the original executor; sample only a successful local query."""

    def __init__(self, executor, read_local):
        self.executor = executor
        self.read_local = read_local
        self.samples = {}

    def __getattr__(self, name):
        return getattr(self.executor, name)

    def receive(self, command, received_at_ns, packet_uid, source_node_id):
        ack = self.executor.receive(command, received_at_ns, packet_uid, source_node_id)
        action = command.get("action", {}) if isinstance(command, dict) else {}
        if not isinstance(action, dict) or action.get("op") != "query":
            return ack
        cid = ack["command_id"]
        if ack["status"] == "applied":
            sid, epoch = action["service_id"], action["epoch"]
            self.samples[cid] = {
                "service_id": sid, "epoch": epoch, "observed_at_ns": ack["executed_at_ns"],
                "request_packet_uid": packet_uid, "result": self.read_local(sid, epoch),
            }
        if ack["status"] == "applied" or (
            ack["status"] == "duplicate" and ack["original_status"] == "applied"
        ):
            # A duplicate replays the first sample, including its original clock and packet UID.
            ack["b03_readback"] = deepcopy(self.samples[cid])
        return ack


class ControlPort:
    """The work order receives only delivered caches and the single packet command transmitter."""

    def __init__(self, controller):
        self.link_reports = SimpleNamespace(snapshot=controller.link_reports.snapshot)
        self.service_reports = SimpleNamespace(snapshot=controller.service_reports.snapshot,
                                               get_spec=controller.service_reports.get_spec)
        self.send_command = controller.send_command
        self.log_control = controller._controller_log
        self.options = deepcopy(controller.options)


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


def attach_device_readers(sim):
    """Build one read-only port per node before the simulator advances."""

    def port(node, executor):
        def read(sid, epoch):
            key = (node, sid, epoch)
            route = sim.rules.get(key)
            local_source = None
            if node == executor.service_specs[sid].src_node_id:
                source, demand = sim.sources[sid], executor.demand_state(sid)
                local_source = {
                    "enabled": executor.source_enabled.get(sid, False),
                    "active_epoch": executor.active_epochs.get(sid, 0),
                    "epoch": source.epoch, "rate_bps": source.rate_bps, "active": source.active,
                    "demand_revision": demand["revision"], "demand_active": demand["active"],
                    "registered": demand["registered"],
                }
            return {
                "prepared": rule_fields(executor.prepared.get((sid, epoch))),
                "installed": rule_fields(executor.installed.get((sid, epoch))),
                "forwarding": None if route is None else {"next_node_id": route[0], "link_id": route[1]},
                "retired": key in sim.retired, "source": local_source,
            }
        return read

    for node, executor in list(sim.control.node_executors.items()):
        sim.control.node_executors[node] = ReportingExecutor(executor, port(node, executor))


def bind_work_loop(sim, roles, emit, *, defer_unreachable_cleanup=False):
    """Attach the adapter immediately after Simulation construction, before the first advance."""
    from packet_device import WorkLoop
    control = sim.control
    if (sim.config.get("mode") != "control" or control is None or sim.now != 0
            or not isinstance(control.loop, ControlLoop) or isinstance(control.loop, WorkLoop)
            or control.loop.command_count or control.loop.last_decision_ns != -10**18):
        raise ValueError("requires a fresh control-mode Simulation")
    if not callable(emit) or not callable(getattr(roles, "plan", None)) or not callable(getattr(roles, "verify", None)):
        raise TypeError("expected an event sink and plan/verify roles")
    loop = WorkLoop(ControlPort(control), roles, emit, defer_unreachable_cleanup=defer_unreachable_cleanup)
    attach_device_readers(sim)
    control.loop = loop
    return loop


class MeasurementMixin:
    """Observe native transitions. A named receive fault is optional in a separate run."""
    def __init__(self, *args, drop_install_ack=False, **kwargs):
        self.meter = None
        self._meter_at = None
        self.drop_install_ack = drop_install_ack
        self.dropped_ack = None
        super().__init__(*args, **kwargs)

    def connect_work_order(self, roles, emit, *, defer_unreachable_cleanup=False):
        return bind_work_loop(self, roles, emit, defer_unreachable_cleanup=defer_unreachable_cleanup)

    def meter_snapshot(self):
        sim = self
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
        return nodes, links, requests, assignments

    def payload_wire_bps(self, rate):
        return required_wire_bps(rate, self.config["payload_bytes"], 8)

    def meter_interval(self, row):
        pass

    def finish_meter(self):
        return self.meter.close()

    def close_meter(self):
        self.meter.file.close()

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


class MeasuredSimulation(MeasurementMixin, Simulation):
    """The accepted native packet simulator with the shared business meter."""


class PublicSimulation(MeasurementMixin, Simulation):
    """Native engine behind the four public data ports."""

    def __init__(self, *args, port_provider=None, **kwargs):
        from simulator_ports import Ports
        self.ports = Ports(port_provider)
        super().__init__(*args, **kwargs)

    def meter_interval(self, row):
        self.ports.observe("meter", {
            "startNs": row["start_ns"], "endNs": row["end_ns"],
            "activeCount": len(row["active"]), "unmetCount": len(row["unmet"]),
            "unmetServiceNs": row["U_service_ns"], "weightedUnmetServiceNs": row["W_service_ns"],
        })

    def _physical_batch(self, row):
        changes = []
        nodes = set()
        for operation in row["operations"]:
            if operation["kind"] != "official_event":
                continue
            data = operation["payload"]
            item = {"type": data["eventType"]}
            if item["type"] in ("link_state", "link_param_change"):
                lid = data["attrs"]["linkId"]
                endpoints = sorted(self.links[lid].endpoints)
                item.update(linkId=lid, endpointIds=endpoints)
                nodes.update(endpoints)
            changes.append(item)
        now = row["time_ns"]
        enabled = self.control is not None and self.start_ns <= now < self.end_ns
        event = {"sequence": row["sequence"], "timeNs": now, "changes": changes,
                 "probeNodes": sorted(nodes) if enabled else [],
                 "remainingNs": max(0, self.end_ns - now) if enabled else 0}
        for node, delay in self.ports.event(event):
            details = {"sequence": row["sequence"], "nodeId": node, "changedAtNs": now, "dueNs": now + delay}
            self.log("simulator-events", {"event": "probe_requested", **details})
            self.schedule(now + delay, partial(self._sample_port, details))

    def _sample_port(self, details):
        node = details["nodeId"]
        online = self.state.node_online[node]
        self.log("simulator-events", {**details, "event": "probe_sampled" if online else "probe_skipped_offline",
                                      "timeNs": self.now})
        if online:
            self.control.send_probes(node)

    def log(self, name, row):
        super().log(name, row)
        if name == "batches":
            self._physical_batch(row)
        elif name == "controller" and row["event"] in ("observation", "service_observation"):
            msg = row["probe"] if row["event"] == "observation" else row["message"]
            self.ports.observe("observation", {
                "kind": msg["kind"], "sampledAtNs": msg["observed_at_ns"], "arrivedAtNs": row["time_ns"],
                "packetUid": row["packet_uid"], "nodeId": msg["node_id"], "sequence": msg["seq"],
                "accepted": row["result"]["accepted"], "reason": row["result"]["reason"], "message": msg,
            })
        elif name == "executions":
            cmd = row["command"]
            self.ports.observe("device", {
                "phase": "execution", "timeNs": row["time_ns"], "nodeId": row["node_id"],
                "commandId": cmd.get("command_id"), "operation": cmd.get("action", {}).get("op"),
                "status": row["status"], "executedAtNs": row["executed_at_ns"],
                "version": row["after_version"], "packetUid": row["packet_uid"],
            })
        elif name == "controller" and row["event"] == "ack_received":
            ack = row["ack"]
            self.ports.observe("device", {
                "phase": "ackArrived", "timeNs": row["time_ns"], "nodeId": ack["node_id"],
                "commandId": ack["command_id"], "status": ack["status"], "executedAtNs": ack["executed_at_ns"],
                "version": ack["version"], "packetUid": row["packet_uid"], "readback": ack.get("b03_readback"),
            })


def public_type(registration, factory):
    if factory is None:
        return PublicSimulation

    class RegisteredSimulation(PublicSimulation):
        def __init__(self, *args, **kwargs):
            settings = deepcopy(registration.get("settings", {}))
            provider = factory(settings)
            identity = (getattr(provider, "id", None), getattr(provider, "version", None),
                        getattr(provider, "contractVersion", None))
            if identity != (registration["id"], registration["version"], registration["contractVersion"]):
                raise ValueError("Public simulator identity differs from its registration")
            super().__init__(*args, port_provider=provider, **kwargs)

    return RegisteredSimulation
