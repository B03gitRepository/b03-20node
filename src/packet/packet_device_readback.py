"""Device-local evidence attached to replies to native, independently issued queries."""


FIELDS = ("next_node_id", "link_id", "reserved_bps", "lease_until_ns")


def rule_fields(record):
    return None if record is None else {key: record[key] for key in FIELDS}


def ack_identity(ack, command, network_source):
    return (
        ack.get("kind") == "ack" and ack.get("command_id") == command["command_id"]
        and ack.get("plan_id") == command["plan_id"]
        and ack.get("node_id") == network_source == command["target_node_id"]
        and type(ack.get("version")) is int and ack["version"] >= 0
    )


def status_time(ack):
    if ack.get("status") == "duplicate":
        return ack.get("original_status"), ack.get("original_executed_at_ns")
    return ack.get("status"), ack.get("executed_at_ns")


def query_receipt(ack, command, source, now, work_id, phase, ttl):
    if not ack_identity(ack, command, source):
        return None
    status, sampled = status_time(ack)
    action = command["action"]
    sample = ack.get("b03_readback")
    if (status != "applied" or type(sampled) is not int or not isinstance(sample, dict)
            or not command["issued_at_ns"] <= sampled <= now
            or sampled > command["issued_at_ns"] + command["validity_ns"] or now - sampled > ttl
            or sample.get("service_id") != action["service_id"] or sample.get("epoch") != action["epoch"]
            or sample.get("observed_at_ns") != sampled or type(sample.get("request_packet_uid")) is not int):
        return None
    state = sample.get("result")
    if not isinstance(state, dict) or not {"source", "forwarding", "retired", "prepared", "installed"} <= state.keys():
        return None
    local_source = state["source"]
    if local_source is not None and (
        not isinstance(local_source, dict)
        or local_source.get("enabled") is not ack.get("source_enabled")
        or local_source.get("active_epoch") != ack.get("active_epoch")
    ):
        return None
    return {
        "work_id": work_id, "plan_id": command["plan_id"], "phase": phase,
        "service_id": action["service_id"], "epoch": action["epoch"],
        "node_id": source, "network_source": source, "query_id": command["command_id"],
        "request_packet_uid": sample["request_packet_uid"], "issued_at_ns": command["issued_at_ns"],
        "observed_at_ns": sampled, "arrived_at_ns": now, "version": ack["version"], "result": state,
    }


def phase_matches(phase, decision, epoch, node, state):
    nodes = decision["node_path"]
    source = state["source"]
    if (node == nodes[0]) != (source is not None):
        return False
    if phase in ("release", "abort"):
        clear = (state["retired"] is True and state["forwarding"] is None
                 and state["prepared"] is None and state["installed"] is None)
        return clear and (source is None or (source.get("enabled") is False and source.get("rate_bps") == 0))
    if source is not None and not (
        source.get("enabled") is True and source.get("active") is True and source.get("demand_active") is True
        and source.get("registered") is True and source.get("epoch") == source.get("active_epoch") == epoch
        and source.get("rate_bps") == decision["reserved_bps"]
        and source.get("demand_revision") == decision["demand_revision"]
    ):
        return False
    return path_rule_matches(decision, node, state)


def refresh_matches(decision, epoch, node, state, demand):
    """A retired demand may have stopped its source while its old forwarding still occupies the ledger."""
    if not demand or not demand["fresh"] or not path_rule_matches(decision, node, state):
        return False
    source = state["source"]
    if node != decision["node_path"][0]:
        return source is None
    if not isinstance(source, dict) or source.get("demand_revision") != demand["revision"]:
        return False
    if source.get("registered") is not demand["registered"] or source.get("demand_active") is not demand["active"]:
        return False
    if demand["revision"] == decision["demand_revision"] and demand["registered"] and demand["active"]:
        return phase_matches("install", decision, epoch, node, state)
    return (source.get("enabled") is False and source.get("rate_bps") == 0
            and source.get("epoch") == source.get("active_epoch") == epoch)


def path_rule_matches(decision, node, state):
    nodes = decision["node_path"]
    if state["retired"] is not False:
        return False
    if node == nodes[-1]:
        return all(state[k] is None for k in ("forwarding", "installed", "prepared"))
    i = nodes.index(node)
    hop = {"next_node_id": nodes[i + 1], "link_id": decision["link_path"][i]}
    prepared, installed = state["prepared"], state["installed"]
    return (
        state["forwarding"] == hop and isinstance(prepared, dict) and isinstance(installed, dict)
        and installed == prepared and all(installed.get(k) == v for k, v in hop.items())
        and installed.get("reserved_bps") == decision["reserved_bps"]
    )


def operation_matches(action, state):
    """Postcondition of one uncertain native write at the queried node."""
    op, epoch, source = action["op"], action["epoch"], state["source"]
    if op in ("prepare", "install"):
        expected = rule_fields(action)
        if state["retired"] or state["prepared"] != expected:
            return False
        if op == "prepare":
            return True
        return (state["installed"] == expected and state["forwarding"] ==
                {"next_node_id": action["next_node_id"], "link_id": action["link_id"]})
    if op == "activate":
        return (source is not None and source.get("enabled") is True and source.get("epoch") == epoch
                and source.get("active_epoch") == epoch and source.get("rate_bps") == action["reserved_bps"]
                and source.get("active") is True and source.get("demand_active") is True
                and source.get("registered") is True and source.get("demand_revision") == action["demand_revision"])
    if op == "deactivate":
        return (source is not None and source.get("enabled") is False and source.get("rate_bps") == 0
                and source.get("active_epoch") == epoch)
    return (state["retired"] is True and all(state[k] is None for k in ("forwarding", "prepared", "installed"))
            and (source is None or source.get("active_epoch") != epoch or source.get("enabled") is False))
