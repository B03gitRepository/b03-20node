"""Reconcile held packet-device cleanups against chronological work and device receipts."""
from collections import Counter


def _require(value, message):
    if not value:
        raise ValueError("Deferred cleanup: " + message)


def _wire(ledger, decision, factor):
    for link in decision["link_path"]:
        ledger[link] += factor * decision["wire_bps"]
        _require(ledger[link] >= 0, "negative link reservation")


def cleanup_budgets(acts, groups, conditions, probes):
    """Return the checked complete ledger immediately before each recovered retirement."""
    owners = {row["work_id"] for row in acts if row["event"] == "cleanup_deferred"}
    result = {}
    if not owners:
        return result
    _require(conditions.get("deviceRecovery") == {"contractVersion": "b03.deferred-cleanup.v1",
                                                  "deferUnreachableCleanup": True}, "cleanup isolation was not selected")
    controls = conditions["simulation"]["control"]
    for wid in owners:
        events = groups[wid]
        start, = events["work_started"]
        step, = events["work_step"]
        old = step["decision"]
        sid = step["service_id"]
        _require(start["release_ids"] == [sid] and not start["install_ids"] and step["phase"] == "release",
                 "only a single terminal release may yield its device slot")
        balance = Counter(step["retained_wire_bps"])
        _require(old["wire_bps"] > 0 and all(balance[link] >= old["wire_bps"] for link in old["link_path"]),
                 "old epoch lacks its full wire reservation")
        # No other operation can execute until the original transaction is parked.
        first = events["cleanup_deferred"][0]
        scan = False
        held = None
        pending_query = set()
        ready = False
        source = old["node_path"][0]
        stop_acks = {e["time_ns"] for e in events["device_ack"] if e["plan_id"] == step["plan_id"]
                     and e["op"] == "deactivate" and e["node_id"] == source and e["service_id"] == sid
                     and e["epoch"] == first["epoch"] and e["original_status"] == "applied"}
        stop_reads = {e["time_ns"] for e in events["device_readback"] if e["phase"] == "recovery"
                      and e.get("op") == "deactivate" and e["node_id"] == e["network_source"] == source
                      and e["service_id"] == sid and e["epoch"] == first["epoch"] and e["matched"]}
        stopped = any(e.get("phase") == "source_stop_ack" and e["plan_id"] == step["plan_id"]
                      and step["time_ns"] <= e["time_ns"] <= first["time_ns"]
                      and e["time_ns"] in (stop_reads if e["reconciled_by_query"] else stop_acks)
                      for e in events["native_transaction"])
        _require(stopped and first["time_ns"] >= step["time_ns"] + controls["transaction_timeout_ns"],
                 "missing source-stop acknowledgment or transaction timeout")
        for row in acts:
            if row is first:
                scan = True
            if not scan:
                continue
            event, now = row["event"], row["time_ns"]
            if row.get("work_id") != wid:
                if event == "work_step":
                    decision = row["decision"]
                    _require(row["service_id"] != sid and held["node_id"] not in decision["node_path"]
                             and set(old["link_path"]).isdisjoint(decision["link_path"]), "conflicting foreign work")
                if event == "device_command" and row["op"] != "query":
                    _require(row["service_id"] != sid and row["node_id"] != held["node_id"], "conflicting foreign write")
                if event == "native_transaction" and row["phase"] == "begin":
                    _wire(balance, row["decision"], 1)
                if event in ("service_released", "service_aborted"):
                    _wire(balance, row["decision"], -1)
            elif event == "cleanup_deferred":
                held = row
                original = next((c for c in events["device_command"] if c["command_id"] == row["expired_command_id"]
                                 and c["plan_id"] == step["plan_id"] and c["op"] == "cleanup" and not c["retry"]), None)
                _require(original and original["service_id"] == sid and original["epoch"] == row["epoch"]
                         and original["node_id"] == row["node_id"] and row["protected_services"] == [sid]
                         and row["protected_nodes"] == [row["node_id"]] and row["protected_links"] == old["link_path"]
                         and original["issued_at_ns"] + controls["command_validity_ns"] < now, "invalid expired write scope")
                _require(any(r["operation"] == "query" and original["issued_at_ns"] < r["time_ns"] <= now
                             and r["plan_id"] == step["plan_id"] for r in events["native_command_timeout"]),
                         "missing timed-out read-only reconciliation")
                ready = False
                pending_query.clear()
            elif event == "cleanup_recovery_probe":
                _require(held and row["node_id"] == held["node_id"] and row["epoch"] == held["epoch"], "wrong restart target")
                receipt = probes.get((row["link_id"], row["node_id"], row["seq"]))
                _require(receipt and receipt["result"]["accepted"] and receipt["probe"]["observed_at_ns"] == row["sampled_at_ns"]
                         and receipt["time_ns"] == row["arrived_at_ns"] and held["time_ns"] < row["sampled_at_ns"]
                         <= row["arrived_at_ns"] <= now and now-row["sampled_at_ns"] <= controls["observation_ttl_ns"],
                         "restart lacks a fresh delivered device probe")
                pending_query.add(None)
            elif event == "device_command":
                if not ready:
                    _require(None in pending_query and row["op"] == "query" and row["service_id"] == sid
                             and row["node_id"] == held["node_id"] and row["epoch"] == held["epoch"],
                             "write or wrong query before reconciliation")
                    pending_query.add(row["command_id"])
            elif event == "device_readback" and row["phase"] == "recovery" and not ready:
                _require(row["query_id"] in pending_query and row["reconciles_command_id"] == held["expired_command_id"]
                         and row["network_source"] == row["node_id"] == held["node_id"] and row["service_id"] == sid
                         and row["epoch"] == held["epoch"] and row["issued_at_ns"] <= row["observed_at_ns"]
                         <= row["arrived_at_ns"] == now, "invalid reconciliation receipt")
                ready = True
            elif event == "service_released":
                _require(ready and row["service_id"] == sid and row["epoch"] == held["epoch"], "premature retirement")
                result[wid] = balance.copy()
                _wire(balance, old, -1)
                _require(balance == Counter(row["retained_wire_bps"]), "reservation after retirement differs")
                break
            if "retained_wire_bps" in row:
                _require(balance == Counter(row["retained_wire_bps"]), "wire changed outside verified transitions")
    return result
