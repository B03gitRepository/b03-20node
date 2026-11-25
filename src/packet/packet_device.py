"""B03 joint work order over the delivered observations and native packet-device protocol.

Install on a newly initialised control-mode Simulation. The controller loop has no
simulator or node handle. Only device-local query sampling needs access to local
data-plane state. Work order accounting uses replies delivered by the original network.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import b03_formulas as formula
from inspect import Parameter, signature

from resnet_bridge import ControlLoop, is_active_demand, validate_install_choice

from packet_device_readback import (
    ack_identity, operation_matches, phase_matches, query_receipt, refresh_matches, status_time,
)


class WorkLoop(ControlLoop):
    def __init__(self, control, roles, emit, *, defer_unreachable_cleanup=False):
        super().__init__(control.link_reports, control.service_reports, self._send, self._log, control.options)
        self.roles, self.emit = roles, emit
        self.transport, self.native_log = control.send_command, control.log_control
        self.work = None
        self.readout = None
        self.native_plan_id = None
        self.last_result = None
        self.last_verified = None
        self.refresh_blocks = {}
        self.defer_unreachable_cleanup = defer_unreachable_cleanup
        self.deferred_cleanups = {}
        self.refresh_completed = 0
        self.refresh_failures = 0
        self.force_review = False
        self.reassessment_after_release = None
        self.verify_parameters = signature(roles.verify).parameters
        self.last_inputs = None
        self.filtered_ticks = 0
        self.planning_calls = 0
        self.trigger_counts = Counter()
        self.last_trigger = None
        self.review_busy_until_ns = 0
        self.retry_at_ns = None
        self.retry_versions = {}
        self.retry_delay_ns = self.config["query_interval_ns"]
        self.now_ns = 0

    def _event(self, event, now, **fields):
        self.emit({"event": event, "time_ns": now, "work_id": self.work["work_id"] if self.work else None, **fields})

    def _log(self, row):
        if self.work and "plan_id" in row:
            # Native plans are numbered per service; all their wire IDs carry this joint work ID.
            row = dict(row, work_id=self.work["work_id"], plan_id=self.native_plan_id)
        self.native_log(row)
        if (self.defer_unreachable_cleanup and row["event"] == "command_timeout" and row.get("operation") == "query"
                and self.transaction and self.pending):
            query, original = self.pending["command"], self.transaction.get("uncertain_command")
            if (original and original["action"]["op"] == "cleanup" and query["command_id"] == row["command_id"]
                    and query["action"]["op"] == "query" and query["target_node_id"] == original["target_node_id"]
                    and query["action"]["service_id"] == original["action"]["service_id"]
                    and query["action"]["epoch"] == original["action"]["epoch"]):
                self.transaction["cleanup_query_timeout"] = (original["command_id"], query["command_id"])
        if row["event"] in {"transaction", "command_timeout"}:
            self._event("native_" + row["event"], row["time_ns"], **{
                key: value for key, value in row.items() if key not in {"event", "time_ns", "work_id"}
            })

    def _send(self, command, retry=False):
        self._event("device_command", self.now_ns, plan_id=command["plan_id"], command_id=command["command_id"],
                    service_id=command["action"]["service_id"], epoch=command["action"]["epoch"],
                    op=command["action"]["op"], node_id=command["target_node_id"], retry=retry,
                    issued_at_ns=command["issued_at_ns"], base_version=command["base_version"],
                    new_version=command["new_version"])
        return self.transport(command, retry=retry)

    def _issue(self, now, node, action):
        self.transaction["plan_id"] = self.native_plan_id
        super()._issue(now, node, action)

    def _snapshots(self, now):
        links, demands = self.link_reports.snapshot(now), self.service_reports.snapshot(now)
        for node, version in links["node_versions"].items():
            self.versions[node] = max(self.versions.get(node, 0), version)
        # Keep the native fresh-probe view intact. Some arrived ACKs are newer than its last probe.
        links["controller_known_node_versions"] = dict(self.versions)
        if self.deferred_cleanups:
            nodes, links_on_hold = self._held_resources()
            held = {sid for item in self.deferred_cleanups.values() for sid in item["services"]}
            held.update(sid for sid, record in self.confirmed.items()
                        if nodes.intersection(record["decision"]["node_path"])
                        or links_on_hold.intersection(record["decision"]["link_path"]))
            held.update(sid for sid, row in demands["services"].items()
                        if nodes.intersection((row["service"]["src_node_id"], row["service"]["dst_node_id"])))
            links["controller_deferred_writes"] = sorted(held)
        return links, demands

    def _install_guard(self, choice, links, demands, now):
        sid = choice["service_id"]
        row = demands["services"].get(sid)
        if not (is_active_demand(row) and row["observed_at_ns"] <= row["received_at_ns"] <= now
                and row["revision"] == choice.get("demand_revision")):
            raise ValueError("no_fresh_matching_demand")
        service = self.service_reports.get_spec(sid)
        validate_install_choice(choice, service, links, now, self.config["qos_profile"])

    def _capacity_guard(self, loads, link_ids, links):
        for lid in link_ids:
            row = links["links"][lid]
            if not row["usable"] or not formula.within_capacity(loads[lid], formula.capacity_budget(
                    row["capacity_bps"], self.config["control_headroom_fraction"])):
                raise ValueError("shared_wire_capacity:" + lid)

    @staticmethod
    def _add(loads, decision, sign=1):
        for lid in decision["link_path"]:
            loads[lid] += sign * decision["wire_bps"]

    @staticmethod
    def _same(old, choice):
        return (choice["accepted"] and
                all(old["decision"].get(key) == choice.get(key)
                    for key in ("node_path", "link_path", "reserved_bps", "wire_bps", "demand_revision")))

    def _order(self, plan, links, demands, now):
        if not isinstance(plan.get("work_id"), str) or not plan["work_id"]:
            raise ValueError("missing_work_id")
        choices, allowed = plan["decisions"], plan["authorized"]
        if not isinstance(choices, dict) or not isinstance(allowed, (list, tuple)) or len(allowed) != len(set(allowed)):
            raise ValueError("invalid_work_scope")
        if not set(allowed) <= choices.keys():
            raise ValueError("missing_authorized_decision")
        release, deploy = [], []
        for sid in allowed:
            choice, old = choices[sid], self.confirmed.get(sid)
            row = demands["services"].get(sid)
            if (not row or choice.get("service_id") != sid or type(choice.get("accepted")) is not bool
                    or type(choice.get("demand_revision")) is not int or choice["demand_revision"] != row["revision"]):
                raise ValueError("decision_demand_binding:" + sid)
            if old and self._same(old, choice):
                continue
            if old:
                release.append(sid)
            if choice["accepted"]:
                deploy.append(sid)
        # RC can diagnose a service without authorizing any change. Explicit execution
        # lists take precedence over deriving a change from that descriptive decision.
        for key, inferred in (("release_ids", release), ("install_ids", deploy)):
            ids = plan.get(key, inferred)
            if not isinstance(ids, (tuple, list)) or len(ids) != len(set(ids)) or not set(ids) <= set(allowed):
                raise ValueError("invalid_" + key)
        release = list(plan.get("release_ids", release))
        deploy = list(plan.get("install_ids", deploy))
        if set(release) - self.confirmed.keys() or any(choices[s]["accepted"] and s not in deploy for s in release):
            raise ValueError("release_decision_conflict")
        if any(not choices[s]["accepted"] or (s in self.confirmed and s not in release) for s in deploy):
            raise ValueError("install_decision_conflict")
        lifecycle = plan.get("lifecycle_release")
        if lifecycle and (lifecycle["release_ids"] != release or set(allowed) != set(release) or deploy):
            raise ValueError("lifecycle_release_scope_conflict")
        scope = plan.get("scope")
        if scope is not None and set(scope["changedServiceIds"]) != set(release) | set(deploy):
            raise ValueError("work_scope_conflict")
        loads = Counter(self.budgets())
        for sid in release:
            row = demands["services"][sid]
            if lifecycle and row["registered"] and row["active"]:
                raise ValueError("active_lifecycle_release:" + sid)
            if not row["fresh"] or row["received_at_ns"] > now:
                raise ValueError("stale_release_authority:" + sid)
            self._add(loads, self.confirmed[sid]["decision"], -1)
        new_links = set()
        dependencies = {}
        for sid in deploy:
            choice = choices[sid]
            self._install_guard(choice, links, demands, now)
            self._add(loads, choice)
            new_links.update(choice["link_path"])
            deps = [old for old in release if old == sid or
                    set(self.confirmed[old]["decision"]["link_path"]).intersection(choice["link_path"])]
            dependencies[sid] = deps
        self._capacity_guard(loads, new_links, links)
        wall = plan.get("decision_wall_ns", plan.get("solver_wall_ns", 0))
        if type(wall) is not int or wall < 0:
            raise ValueError("invalid_solver_duration")
        return {
            "work_id": plan["work_id"], "snapshot_id": plan.get("snapshot_id"), "authorized": list(allowed),
            "decisions": deepcopy(choices), "release_ids": release, "install_ids": deploy, "dependencies": dependencies,
            "queue": [("release", sid) for sid in release] + [("install", sid) for sid in deploy],
            "completed": {}, "failures": {}, "receipts": {},
            "planned_at_ns": now, "solver_wall_ns": plan.get("solver_wall_ns", 0), "decision_wall_ns": wall, "solver_ready_at_ns": now + wall,
            "native_plan_ids": [], "joint_rechecked": False, "repair_queue": [], "lifecycle_release": lifecycle,
        }

    def _configuration_key(self, links, demands, now):
        """Only freshness/continuity of received evidence enters the trigger, never a periodic sequence or clock."""
        ttl = self.config["observation_ttl_ns"]
        probes = {}
        for link in links["links"].values():
            for node, item in link["reporters"].items():
                if item is None:
                    continue
                p = item["probe"]
                if p["observed_at_ns"] <= item["received_at_ns"] <= now and now - p["observed_at_ns"] <= ttl:
                    probes[node] = max(probes.get(node, (-1, -1)), (p["version"], p["observed_at_ns"]))
        result = {}
        for sid, entry in self.confirmed.items():
            demand = demands["services"].get(sid)
            states = []
            for node in entry["decision"]["node_path"]:
                receipt = entry.get("readback_nodes", {}).get(node)
                if not receipt or not receipt["matched"]:
                    states.append(False)
                    continue
                sampled, version = receipt["observed_at_ns"], receipt["version"]
                probe_version, probe_time = probes.get(node, (-1, -1))
                covered = (self.versions.get(node, version) <= version and
                           (now - sampled <= ttl or (probe_version == version and probe_time >= sampled)))
                if node == entry["decision"]["node_path"][0]:
                    source = receipt["result"]["source"]
                    covered = (covered and demand is not None and demand["fresh"] and source is not None
                               and source["demand_revision"] == demand["revision"])
                states.append(bool(covered))
            result[sid] = tuple(states)
        return result

    def _gate(self, links, demands, now):
        # Compare decision semantics at every native control tick. Sequence numbers,
        # periodic sample clocks and FIFO occupancy do not affect this B03 solver.
        physical = ("capacity_bps", "delay_ns", "jitter_ns", "ber", "packet_loss_rate")
        link_key = {}
        for lid, row in links["links"].items():
            key = (row["usable"], row["reason"])
            link_key[lid] = key + tuple(row.get(k) for k in physical) if row["usable"] else key
        demand_key = {sid: (row["fresh"], row["registered"], row["active"], row["revision"],
                            tuple(sorted(row["service"].items()))) for sid, row in demands["services"].items()}
        installed = {}
        for sid, record in self.confirmed.items():
            choice = record["decision"]
            installed[sid] = (record["epoch"], choice["demand_revision"], tuple(choice["node_path"]),
                              tuple(choice["link_path"]), choice["reserved_bps"], choice["wire_bps"])
        current = {
            "links": link_key, "demands": demand_key, "confirmed": installed,
            "reservations": self.budgets(), "versions": dict(self.versions),
            "deferred_writes": tuple(links.get("controller_deferred_writes", ())),
            "configuration_evidence": self._configuration_key(links, demands, now),
            "policy": (self.config["control_headroom_fraction"], self.config["qos_profile"],
                       self.roles.planning_key() if callable(getattr(self.roles, "planning_key", None)) else None),
        }
        previous = self.last_inputs
        changes = ["initial"] if previous is None else [k for k, value in current.items() if previous[k] != value]
        if self.force_review:
            changes.append("configuration_refresh_completed")
        if self.reassessment_after_release is not None:
            changes.append("lifecycle_release_confirmed")
        if self.retry_at_ns is not None and now >= self.retry_at_ns:
            changes.append("unconfirmed_work_retry")
        waiting_to_retry = (self.retry_at_ns is not None and now < self.retry_at_ns
                            and changes == ["versions"] and self.versions == self.retry_versions)
        if not changes or now < self.review_busy_until_ns or waiting_to_retry:
            self.filtered_ticks += 1
            return False
        affected = {}
        if previous is not None:
            for k in ("links", "demands", "confirmed", "reservations", "versions", "configuration_evidence"):
                if k in changes:
                    old, new = previous[k], current[k]
                    affected[k] = sorted(i for i in old.keys() | new.keys() if old.get(i) != new.get(i))
        self.force_review = False
        self.last_inputs = current
        self.planning_calls += 1
        self.trigger_counts.update(changes)
        self.last_trigger = {"time_ns": now, "reasons": changes, "changed_ids": affected}
        self.retry_at_ns = None
        self._event("work_trigger", now, reasons=changes, changed_ids=affected,
                    planning_calls=self.planning_calls, filtered_ticks=self.filtered_ticks)
        return True

    def _schedule_retry(self, now):
        self.retry_at_ns = now + self.retry_delay_ns
        self.retry_versions = dict(self.versions)
        self.retry_delay_ns = min(self.retry_delay_ns * 2, max(self.config["observation_ttl_ns"],
                                                              self.config["query_interval_ns"]))

    def status(self):
        wait = []
        if self.readout is not None:
            for (sid, node), item in self.readout["waiting"].items():
                cmd = item["command"]
                wait.append({"node_id": node, "service_id": sid, "command_id": cmd["command_id"] if cmd else None,
                             "phase": self.readout["phase"], "retry_at_ns": item.get("resume_at_ns")})
        work = self.work
        return {
            "planning_calls": self.planning_calls, "filtered_ticks": self.filtered_ticks,
            "trigger_counts": dict(self.trigger_counts), "last_trigger": deepcopy(self.last_trigger),
            "retry_at_ns": self.retry_at_ns, "last_work_result": deepcopy(self.last_result),
            "last_verified_work_id": self.last_verified,
            "deferred_cleanups": [dict(work_id=wid, service_id=item["transaction"]["decision"]["service_id"],
                                       epoch=item["transaction"]["epoch"], node_id=item["node"],
                                       deferred_at_ns=item["since"], protected_nodes=[item["node"]],
                                       protected_links=item["links"])
                                  for wid, item in self.deferred_cleanups.items()],
            "current_work_id": work["work_id"] if work else None, "remaining_operations": list(work["queue"]) if work else [],
            "independent_queries_pending": wait, "pending_native_command": deepcopy(self.pending),
            "transaction_phase": self.transaction["status"] if self.transaction else None,
            "retained_wire_bps": self.budgets(), "known_node_versions": dict(self.versions),
            "configuration_refreshes": self.refresh_completed, "unsuccessful_configuration_refreshes": self.refresh_failures,
            "configuration_refresh_blocked_ids": sorted(self.refresh_blocks),
            "solver_clock": "RC selection and solver wall ns charged before eligibility at the next native control tick",
        }

    def _held_resources(self):
        return ({item["node"] for item in self.deferred_cleanups.values()},
                {link for item in self.deferred_cleanups.values() for link in item["links"]})

    def _defer_expired_cleanup(self, now):
        # The original source has acknowledged stop; the entire old epoch is still charged.
        # Only a timed-out read-only reconciliation can be parked. A live write is never detached.
        tr = self.transaction
        if (not tr or self.pending or self.readout or tr["status"] != "unknown"
                or not tr["source_confirmed"] or now < tr["deadline_ns"]):
            return
        original = tr["uncertain_command"]
        timed_out = tr.get("cleanup_query_timeout")
        if (not timed_out or timed_out[0] != original["command_id"]
                or original["action"]["op"] != "cleanup" or tr["old"] not in self.retained
                or now <= original["issued_at_ns"] + original["validity_ns"]):
            return
        work = self.work
        sid = tr["decision"]["service_id"]
        if work["release_ids"] != [sid] or work["install_ids"] or work["queue"]:
            return
        # Other service/epoch rules have distinct keys at shared healthy routers. Their
        # writes are serialized by the existing per-node version check; no write is in flight here.
        wid, node = work["work_id"], original["target_node_id"]
        links = list(tr["decision"]["link_path"])
        del tr["cleanup_query_timeout"]  # A resumed attempt must earn a new timeout of its own.
        self.deferred_cleanups[wid] = {"work": work, "transaction": tr, "plan_id": self.native_plan_id,
                                       "node": node, "since": now, "services": [sid], "links": links}
        self._event("cleanup_deferred", now, plan_id=self.native_plan_id, service_id=sid,
                    epoch=tr["epoch"], node_id=node, expired_command_id=original["command_id"], timed_out_query_id=timed_out[1],
                    protected_nodes=[node], protected_links=links, protected_services=[sid], retained_wire_bps=self.budgets())
        self.work = self.transaction = self.native_plan_id = None

    def _resume_reachable_cleanup(self, now, links):
        for wid, item in self.deferred_cleanups.items():
            node = item["node"]
            # Only an arrived fresh probe sampled after this attempt can request another query.
            for link in links["links"].values():
                received = link["reporters"].get(node)
                if not received:
                    continue
                sampled, arrived = received["probe"]["observed_at_ns"], received["received_at_ns"]
                if item["since"] < sampled <= arrived <= now and now - sampled <= self.config["observation_ttl_ns"]:
                    break
            else:
                continue
            self.work, self.transaction, self.native_plan_id = item["work"], item["transaction"], item["plan_id"]
            del self.deferred_cleanups[wid]
            self._event("cleanup_recovery_probe", now, node_id=node, sampled_at_ns=sampled, arrived_at_ns=arrived,
                        link_id=received["probe"]["link"]["link_id"], seq=received["probe"]["seq"],
                        epoch=self.transaction["epoch"], retained_wire_bps=self.budgets())
            self._query(now, self.transaction["uncertain_command"])
            return True
        return False

    def tick(self, now):
        self.now_ns = now
        if self.defer_unreachable_cleanup:
            self._defer_expired_cleanup(now)
        if self.readout is not None:
            self._poll_readout(now)
        elif self.pending or self.transaction:
            self._advance_pending(now)
        elif self.work is not None:
            self._advance_work(now)
        else:
            links, demands = self._snapshots(now)
            if self.deferred_cleanups and self.reassessment_after_release is None and self._resume_reachable_cleanup(now, links):
                return
            if not self._gate(links, demands, now):
                return
            self.last_decision_ns = now
            followup = ({} if self.reassessment_after_release is None else
                        {"reassessment_after_release": self.reassessment_after_release})
            plan = self.roles.plan(now, deepcopy(links), deepcopy(demands), deepcopy(self.confirmed),
                                   self.budgets(), **followup)
            self.reassessment_after_release = None
            if plan is None:
                return
            if plan.get("refresh_ids"):
                self._open_refresh(now, plan, links, demands)
                if self.work is not None:
                    self._advance_work(now)
                    return
                # A previously negative readback keeps its old reservation. Independent
                # actions can still proceed under the newly solved explicit write scope.
                if not (plan.get("release_ids") or plan.get("install_ids")):
                    return
            try:
                self.work = self._order(plan, links, demands, now)
            except (ValueError, KeyError, TypeError) as exc:
                self._event("work_rejected", now, work_id=plan.get("work_id"), reason=str(exc))
                self._schedule_retry(now)
                return
            self.review_busy_until_ns = self.work["solver_ready_at_ns"]
            if not self.work["queue"]:
                self.retry_delay_ns = self.config["query_interval_ns"]
                self._event("work_idle", now, snapshot_id=self.work["snapshot_id"], authorized=self.work["authorized"],
                            solver_wall_ns=self.work["solver_wall_ns"])
                self.work = None
                return
            self._event("work_started", now, **{key: self.work[key] for key in
                        ("snapshot_id", "authorized", "release_ids", "install_ids", "dependencies",
                         "solver_wall_ns", "decision_wall_ns", "solver_ready_at_ns")}, budgets_before=self.budgets(),
                        decisions=self.work["decisions"])
            self._advance_work(now)

    def _refresh_key(self, sid, links, demands):
        entry = self.confirmed[sid]
        d = demands["services"].get(sid, {})
        path = entry["decision"]["link_path"]
        physical = ("usable", "reason", "capacity_bps", "delay_ns", "jitter_ns", "ber", "packet_loss_rate")
        return (entry["epoch"], d.get("revision"), d.get("active"), d.get("fresh"),
                tuple(self.versions.get(n, 0) for n in entry["decision"]["node_path"]),
                tuple(tuple(links["links"][lid].get(k) for k in physical) for lid in path),
                self.roles.planning_key() if callable(getattr(self.roles, "planning_key", None)) else None)

    def _open_refresh(self, now, plan, links, demands):
        ids = plan["refresh_ids"]
        if (not isinstance(ids, (list, tuple)) or len(ids) != len(set(ids)) or not set(ids) <= self.confirmed.keys()
                or not isinstance(plan.get("work_id"), str) or not plan["work_id"]
                or set(ids).intersection(set(plan.get("release_ids", ())) | set(plan.get("install_ids", ())))):
            raise ValueError("invalid_read_only_configuration_scope")
        unknown = [sid for sid in ids if self.refresh_blocks.get(sid) != self._refresh_key(sid, links, demands)]
        if not unknown:
            self._event("config_refresh_deferred", now, work_id=plan["work_id"], service_ids=ids,
                        reason="same_unresolved_device_evidence", retained_wire_bps=self.budgets())
            return
        wall = plan.get("decision_wall_ns", plan.get("solver_wall_ns", 0))
        if type(wall) is not int or wall < 0:
            raise ValueError("invalid_solver_duration")
        self.review_busy_until_ns = now + wall
        self.work = {
            "work_id": plan["work_id"], "snapshot_id": plan.get("snapshot_id"), "refresh_ids": unknown,
            "solver_ready_at_ns": now + wall, "queue": [],
        }
        self._event("config_refresh_requested", now, service_ids=unknown, snapshot_id=plan.get("snapshot_id"),
                    deferred_write_ids=sorted(set(plan.get("release_ids", ())) | set(plan.get("install_ids", ()))),
                    retained_wire_bps=self.budgets())

    def _query_confirmed(self, now, ids, *, inside_order):
        targets = {}
        for sid in ids:
            record = self.confirmed[sid]
            self.plan_count += 1
            plan_id = f'{self.work["work_id"]}/Q{self.plan_count:04d}'
            if inside_order:
                self.work["native_plan_ids"].append(plan_id)
            targets[sid] = {"plan_id": plan_id, "decision": record["decision"], "epoch": record["epoch"]}
        self._start_readout(now, "config_refresh", final_targets=targets)
        self.readout["inside_order"] = inside_order

    def _advance_work(self, now):
        work = self.work
        if now < work["solver_ready_at_ns"]:
            return
        if "refresh_ids" in work:
            self._query_confirmed(now, work["refresh_ids"], inside_order=False)
            return
        if work["repair_queue"]:
            self._repair_disputed_install(now, work["repair_queue"].pop(0))
            return
        while work["queue"]:
            phase, sid = work["queue"].pop(0)
            choice = work["decisions"][sid]
            links, demands = self._snapshots(now)
            try:
                if self.deferred_cleanups:
                    route = self.confirmed[sid]["decision"] if phase == "release" else choice
                    nodes, protected_links = self._held_resources()
                    if (sid in links.get("controller_deferred_writes", ()) or nodes.intersection(route["node_path"])
                            or protected_links.intersection(route["link_path"])):
                        raise ValueError("unresolved_cleanup_write_scope")
                if phase == "release":
                    row = demands["services"].get(sid)
                    if (not row or not row["fresh"] or row["received_at_ns"] > now
                            or row["revision"] != choice["demand_revision"]):
                        raise ValueError("release_authority_changed")
                    if work["lifecycle_release"] and row["active"] and row["registered"]:
                        raise ValueError("lifecycle_demand_reactivated")
                    checker = getattr(self.roles, "release_authority", None)
                    if callable(checker):
                        authority = checker(now, deepcopy(links), deepcopy(demands), deepcopy(self.confirmed),
                                            self.budgets(), sid)
                        if authority.get("authorized") is not True:
                            if authority.get("requires_refresh"):
                                if self.refresh_blocks.get(sid) != self._refresh_key(sid, links, demands):
                                    work["queue"].insert(0, (phase, sid))
                                    self._event("release_awaiting_config_refresh", now, service_id=sid,
                                                authority=authority["status"], retained_wire_bps=self.budgets())
                                    self._query_confirmed(now, [sid], inside_order=True)
                                    return
                                work["refresh_needed"] = True
                            raise ValueError("release_no_longer_authorized:" + authority["status"])
                        self._event("release_authority_checked", now, service_id=sid,
                                    status=authority["status"], demand_revision=row["revision"],
                                    authority_snapshot_id=authority.get("snapshot_id"))
                    if choice["accepted"]:
                        self._install_guard(choice, links, demands, now)
                else:
                    if sid in self.confirmed or any("release" not in work["completed"].get(dep, ())
                                                   for dep in work["dependencies"][sid]):
                        raise ValueError("release_dependency_unconfirmed")
                    self._install_guard(choice, links, demands, now)
                    loads = Counter(self.budgets())
                    self._add(loads, choice)
                    self._capacity_guard(loads, choice["link_path"], links)
            except ValueError as exc:
                work["failures"].setdefault(sid, str(exc))
                self._event("work_step_skipped", now, phase=phase, service_id=sid, reason=str(exc))
                continue
            self.native_plan_id = f'{work["work_id"]}/P{self.plan_count + 1:04d}'
            work["native_plan_ids"].append(self.native_plan_id)
            self._event("work_step", now, phase=phase, service_id=sid, plan_id=self.native_plan_id,
                        demand_revision=choice["demand_revision"], retained_wire_bps=self.budgets(),
                        decision=self.confirmed[sid]["decision"] if phase == "release" else choice)
            if phase == "release":
                self._stop(now, sid, choice["reason"])
            else:
                self._begin(now, choice)
            return
        self._finish_work(now)

    def _finish_work(self, now):
        work = self.work
        links, demands = self._snapshots(now)
        active_targets = {}
        for sid in work["install_ids"]:
            if "install" not in work["completed"].get(sid, ()):
                continue
            record = self.confirmed[sid]
            try:
                self._install_guard(record["decision"], links, demands, now)
                self._capacity_guard(Counter(self.budgets()), record["decision"]["link_path"], links)
            except ValueError as exc:
                work["failures"][sid] = "verification:" + str(exc)
            else:
                active_targets[sid] = record
        stale = any(now - row["observed_at_ns"] > self.config["observation_ttl_ns"]
                    or row["version"] < self.versions.get(node, 0)
                    for sid in active_targets for node, row in work["receipts"][sid]["install"]["nodes"].items())
        if stale and not work["joint_rechecked"]:
            # Earlier services may have finished many seconds before the last one. Obtain
            # a common fresh window for the final OM check. Old release samples document the
            # earlier release barrier, even if the same source is now active on its new epoch.
            self._start_readout(now, "install", final_targets=active_targets)
            return
        operations = {key: deepcopy(work[key]) for key in ("release_ids", "install_ids", "completed", "failures")}
        opts = dict(device_receipts=deepcopy(work["receipts"]), expected=deepcopy(work["decisions"]),
                    operations=operations, retained_wire_bps=self.budgets())
        if not any(p.kind == Parameter.VAR_KEYWORD for p in self.verify_parameters.values()):
            opts = {key: value for key, value in opts.items() if key in self.verify_parameters}
        result = self.roles.verify(work["work_id"], now, deepcopy(links), deepcopy(demands), deepcopy(self.confirmed), **opts)
        ok = (not work["failures"] and all(phase in work["completed"].get(sid, ())
              for phase, ids in (("release", work["release_ids"]), ("install", work["install_ids"])) for sid in ids))
        om_confirmed = result.get("configuration_matches") if isinstance(result, dict) else result
        ok = ok and om_confirmed is True
        self.last_result = {
            **operations, "work_id": work["work_id"], "snapshot_id": work["snapshot_id"],
            "verified_at_ns": now, "native_plan_ids": work["native_plan_ids"], "om": result, "verified": bool(ok),
        }
        self._event("work_verified" if ok else "work_incomplete", now, **self.last_result,
                    retained_wire_bps=self.budgets())
        if work["lifecycle_release"] and isinstance(result, dict):
            released = sorted(sid for sid in result["matched"] if sid not in self.confirmed
                              and "release" in work["completed"].get(sid, ()))
            if released:
                self.reassessment_after_release = {"work_id": work["work_id"], "verified_at_ns": now,
                                                   "release_ids": released}
        if ok:
            self.last_verified = work["work_id"]
            self.retry_delay_ns = self.config["query_interval_ns"]
        else:
            self._schedule_retry(now)
            if work.get("refresh_needed"):
                self.retry_at_ns = now
        self.work, self.native_plan_id = None, None

    def _next(self, now):
        tr = self.transaction
        phase, i = tr["status"], tr["index"]
        if phase in {"cleaning", "aborting"} and i >= len(tr["steps"]):
            self._start_readout(now, "abort" if phase == "aborting" or tr.get("rollback") else "release")
            return
        if phase == "deploying" and i < len(tr["steps"]) and tr["steps"][i][1]["op"] == "activate":
            try:
                links, demands = self._snapshots(now)
                self._install_guard(tr["decision"], links, demands, now)
                self._capacity_guard(Counter(self.budgets()), tr["decision"]["link_path"], links)
            except ValueError as exc:
                self._abort(now, str(exc))
                return
        super()._next(now)

    def _abort(self, now, reason):
        self.work["failures"].setdefault(self.transaction["decision"]["service_id"], reason)
        super()._abort(now, reason)

    def _activated(self, now, reconciled=False):
        # Activation ACK is a possible write effect. Keep the in-flight reservation intact.
        self._log({"event": "transaction", "time_ns": now, "plan_id": self.native_plan_id,
                   "phase": "source_write_ack", "status": "awaiting_path_readback", "reconciled_by_query": reconciled})
        self._start_readout(now, "install")

    def _deactivated(self, now, reconciled=False):
        tr = self.transaction
        if tr["old"] is not None and tr["old"] not in self.retained:
            self.retained.append(tr["old"])
        tr.update(status="retaining", source_confirmed=True, resume_at_ns=now + self.config["old_rule_retention_ns"])
        self._log({"event": "transaction", "time_ns": now, "plan_id": self.native_plan_id, "phase": "source_stop_ack",
                   "status": "retaining_until_cleanup_readback", "reconciled_by_query": reconciled})

    def _new_query(self, now, node, target):
        self.command_count += 1
        version = self.versions.get(node, 0)
        return {
            "kind": "command", "command_id": f"C{self.command_count:05d}", "plan_id": target["plan_id"],
            "target_node_id": node, "issued_at_ns": now, "validity_ns": self.config["command_validity_ns"],
            "base_version": version, "new_version": version,
            "action": {"op": "query", "service_id": target["decision"]["service_id"], "epoch": target["epoch"]},
        }

    def _schedule_query(self, now, key):
        sid, node = key
        command = self._new_query(now, node, self.readout["targets"][sid])
        self.readout["waiting"][key] = {"command": command, "retries": 0, "sent_at_ns": now}
        self._send(command)

    def _start_readout(self, now, phase, *, final_targets=None):
        if final_targets is None:
            tr = self.transaction
            tr["status"] = "reading_" + phase
            targets = {tr["decision"]["service_id"]: tr}
        else:
            targets = final_targets
        self.readout = {"phase": phase, "waiting": {}, "received": {}, "targets": targets,
                        "joint_refresh": final_targets is not None, "started_at_ns": now}
        # All requested devices, including source and terminal, reply independently in-band.
        for sid, target in targets.items():
            nodes = target["decision"]["node_path"]
            self._event("readback_requested", now, phase=phase, plan_id=target["plan_id"], epoch=target["epoch"],
                        service_id=sid, nodes=nodes, joint_refresh=final_targets is not None,
                        retained_wire_bps=self.budgets())
            for node in nodes:
                self._schedule_query(now, (sid, node))

    def _poll_readout(self, now):
        if (self.readout["phase"] == "config_refresh"
                and now >= self.readout["started_at_ns"] + self.config["transaction_timeout_ns"]):
            self._finish_configuration_refresh(now)
            return
        for key, item in list(self.readout["waiting"].items()):
            cmd = item["command"]
            if cmd is None:
                if now >= item["resume_at_ns"]:
                    self._schedule_query(now, key)
                continue
            if now - item["sent_at_ns"] < self.config["ack_wait_ns"]:
                continue
            if item["retries"] < self.config["max_retries"] and now <= cmd["issued_at_ns"] + cmd["validity_ns"]:
                item.update(retries=item["retries"] + 1, sent_at_ns=now)
                self._send(cmd, retry=True)
            else:
                self._log({"event": "command_timeout", "time_ns": now, "command_id": cmd["command_id"],
                           "plan_id": cmd["plan_id"], "state": "unknown", "operation": "query"})
                item.update(command=None, resume_at_ns=now + self.config["query_interval_ns"])
                self._event("budget_hold", now, reason="independent_query_timeout", node_id=key[1],
                            service_id=cmd["action"]["service_id"], retained_wire_bps=self.budgets())

    def _readout_ack(self, ack, now, node):
        batch = self.readout
        sample = ack.get("b03_readback")
        if not isinstance(sample, dict) or not isinstance(sample.get("service_id"), str):
            return
        key = (sample["service_id"], node)
        item = batch["waiting"].get(key)
        if not item or not item["command"]:
            return
        target = batch["targets"][key[0]]
        receipt = query_receipt(ack, item["command"], node, now, self.work["work_id"],
                                batch["phase"], self.config["observation_ttl_ns"])
        if receipt is None or receipt["version"] < self.versions.get(node, 0):
            return
        self.versions[node] = receipt["version"]
        if batch["phase"] == "config_refresh":
            demand = self.service_reports.snapshot(now)["services"].get(key[0])
            receipt["matched"] = refresh_matches(target["decision"], target["epoch"], node, receipt["result"], demand)
        else:
            receipt["matched"] = phase_matches(batch["phase"], target["decision"], target["epoch"], node, receipt["result"])
        batch["received"][key] = receipt
        del batch["waiting"][key]
        self._event("device_readback", now, **receipt, joint_refresh=batch["joint_refresh"])
        if batch["waiting"]:
            return
        # Do not join an early, expired sample with a later reply and call it fresh.
        stale = [nid for nid, row in batch["received"].items() if now - row["observed_at_ns"] > self.config["observation_ttl_ns"]]
        if stale:
            for nid in stale:
                del batch["received"][nid]
                self._schedule_query(now, nid)
            return
        if batch["phase"] == "config_refresh":
            self._finish_configuration_refresh(now)
        elif batch["joint_refresh"]:
            for sid, target in batch["targets"].items():
                rows = {n: batch["received"][sid, n] for n in target["decision"]["node_path"]}
                self.work["receipts"][sid]["install"]["nodes"] = rows
                if all(row["matched"] for row in rows.values()):
                    target.update(confirmed_at_ns=now, readback_nodes=deepcopy(rows),
                                  query_ids={n: row["query_id"] for n, row in rows.items()})
                else:
                    self.work["failures"][sid] = "joint_device_readback_mismatch"
                    self.work["completed"][sid].remove("install")
                    self.confirmed.pop(sid)
                    self.retained.append(target)
                    src = rows[target["decision"]["node_path"][0]]["result"]["source"]
                    self.work["repair_queue"].append((target, src))
                self._event("service_rechecked", now, service_id=sid, epoch=target["epoch"],
                            matched=all(row["matched"] for row in rows.values()),
                            query_ids={n: row["query_id"] for n, row in rows.items()})
            self.work["joint_rechecked"] = True
            self.readout = None
            self._advance_work(now)
        else:
            batch["received"] = {n: row for (_, n), row in batch["received"].items()}
            self._finish_readout(now)

    def _finish_configuration_refresh(self, now):
        batch = self.readout
        links, demands = self._snapshots(now)
        good, unknown = [], []
        ttl = self.config["observation_ttl_ns"]
        for sid, target in batch["targets"].items():
            rows = {n: row for (s, n), row in batch["received"].items() if s == sid}
            demand = demands["services"].get(sid)
            for node, row in rows.items():
                row["matched"] = (now - row["observed_at_ns"] <= ttl
                                  and row["version"] >= self.versions.get(node, 0)
                                  and refresh_matches(target["decision"], target["epoch"], node, row["result"], demand))
            record = self.confirmed[sid]
            record["readback_nodes"] = deepcopy(rows)
            record["query_ids"] = {node: row["query_id"] for node, row in rows.items()}
            ok = len(rows) == len(target["decision"]["node_path"]) and all(r["matched"] for r in rows.values())
            record["configuration_checked_at_ns"] = now
            record["configuration_observed"] = ok
            if ok:
                good.append(sid)
                self.refresh_blocks.pop(sid, None)
            else:
                unknown.append(sid)
                self.refresh_blocks[sid] = self._refresh_key(sid, links, demands)
        self.refresh_completed += len(good)
        self.refresh_failures += len(unknown)
        inside = batch.get("inside_order", False)
        self._event("config_refresh_result", now, matched=good, unconfirmed=unknown,
                    retained_wire_bps=self.budgets(), replan_before_write=not inside,
                    recheck_original_authority=inside)
        self.readout = None
        if inside:
            self._advance_work(now)
        else:
            self.work, self.native_plan_id = None, None
            self.force_review = True

    def _repair_disputed_install(self, now, disputed):
        record, source = disputed
        choice, epoch = record["decision"], record["epoch"]
        sid = choice["service_id"]
        self.plan_count += 1
        self.native_plan_id = f'{self.work["work_id"]}/P{self.plan_count:04d}'
        self.work["native_plan_ids"].append(self.native_plan_id)
        self.transaction = {
            "plan_id": self.native_plan_id, "decision": choice, "epoch": epoch, "old": record, "rollback": True,
            "steps": [], "index": 0, "status": "deploying", "touched": list(choice["node_path"]),
            "source_confirmed": False, "deadline_exceeded": False,
            "deadline_ns": now + self.config["transaction_timeout_ns"],
        }
        self._event("budget_hold", now, service_id=sid, epoch=epoch, reason="joint_device_readback_mismatch",
                    retained_wire_bps=self.budgets())
        if source and source["enabled"] and source["active_epoch"] == epoch:
            self.transaction["status"] = "stopping"
            self._issue(now, choice["node_path"][0], {"op": "deactivate", "service_id": sid, "epoch": epoch})
        else:
            self._abort(now, "joint_device_readback_mismatch")

    def _finish_readout(self, now):
        tr, work, batch = self.transaction, self.work, self.readout
        self.readout = None
        phase, rows, choice = batch["phase"], batch["received"], tr["decision"]
        sid, epoch = choice["service_id"], tr["epoch"]
        work["receipts"].setdefault(sid, {})[phase] = {"epoch": epoch, "decision": deepcopy(choice), "nodes": rows}
        bad = [node for node, row in rows.items() if not row["matched"]]
        reason = "device_readback_mismatch" if bad else None
        if phase == "install" and reason is None:
            try:
                links, demands = self._snapshots(now)
                self._install_guard(choice, links, demands, now)
                self._capacity_guard(Counter(self.budgets()), choice["link_path"], links)
            except ValueError as exc:
                reason = str(exc)
        if reason is not None:
            self._event("budget_hold", now, phase=phase, service_id=sid, epoch=epoch, reason=reason,
                        mismatched_nodes=bad, retained_wire_bps=self.budgets())
            source = rows[choice["node_path"][0]]["result"]["source"]
            if phase == "install":
                work["failures"].setdefault(sid, reason)
            if source and source["active_epoch"] == epoch and source["enabled"]:
                # Never remove intermediate forwarding while the affected source may still be transmitting.
                tr["rollback"] = phase != "release"
                tr["old"] = tr.get("old") or {"epoch": epoch, "decision": choice}
                tr["status"] = "stopping"
                self._issue(now, choice["node_path"][0], {"op": "deactivate", "service_id": sid, "epoch": epoch})
            elif phase == "install":
                self._abort(now, reason)
            else:
                op = "cleanup" if phase == "release" else "abort"
                tr.update(steps=[(node, {"op": op, "service_id": sid, "epoch": epoch}) for node in reversed(bad)],
                          index=0, status="cleaning" if phase == "release" else "aborting")
                self._next(now)
            return
        if phase == "install":
            self.confirmed[sid] = {
                "epoch": epoch, "decision": deepcopy(choice), "confirmed_at_ns": now, "plan_id": tr["plan_id"],
                "readback_nodes": deepcopy(rows), "configuration_checked_at_ns": now, "configuration_observed": True,
                "query_ids": {node: row["query_id"] for node, row in rows.items()},
            }
            self._log({"event": "transaction", "time_ns": now, "plan_id": tr["plan_id"], "phase": "source_confirmed",
                       "status": "confirmed", "epoch": epoch, "service_id": sid, "by": "all_device_queries"})
            event = "service_confirmed"
        else:
            if self.confirmed.get(sid, {}).get("epoch") == epoch:
                self.confirmed.pop(sid)
            self.refresh_blocks.pop(sid, None)
            self.retained[:] = [record for record in self.retained
                                if (record["decision"]["service_id"], record["epoch"]) != (sid, epoch)]
            self._log({"event": "transaction", "time_ns": now, "plan_id": tr["plan_id"],
                       "phase": "cleanup_complete" if phase == "release" else "abort_complete",
                       "status": "confirmed" if phase == "release" else "aborted", "epoch": epoch,
                       "service_id": sid, "by": "all_device_queries"})
            event = "service_released" if phase == "release" else "service_aborted"
        if phase != "abort":
            work["completed"].setdefault(sid, []).append(phase)
        self.transaction = None
        self._event(event, now, phase=phase, service_id=sid, epoch=epoch, decision=choice, plan_id=tr["plan_id"],
                    query_ids={node: row["query_id"] for node, row in rows.items()}, retained_wire_bps=self.budgets())

    def receive_ack(self, ack, now, source_node):
        self.now_ns = now
        if self.readout is not None:
            self._readout_ack(ack, now, source_node)
            return
        if not self.pending:
            return
        cmd = self.pending["command"]
        if not ack_identity(ack, cmd, source_node):
            return
        status, sampled = status_time(ack)
        if cmd["action"]["op"] == "query":
            self._recovery_ack(ack, cmd, now, source_node)
            return
        if status not in {"applied", "rejected"}:
            return
        if status == "applied":
            if (type(sampled) is not int or not cmd["issued_at_ns"] <= sampled <= now
                    or sampled > cmd["issued_at_ns"] + cmd["validity_ns"]):
                return
            action = cmd["action"]
            if (ack["version"] != cmd["new_version"] or
                    (action["op"] == "activate" and (ack.get("source_enabled") is not True
                                                    or ack.get("active_epoch") != action["epoch"]))):
                self.pending = None
                self._query(now, cmd)
                return
        self._event("device_ack", now, service_id=cmd["action"]["service_id"], epoch=cmd["action"]["epoch"],
                    node_id=source_node, command_id=cmd["command_id"], plan_id=cmd["plan_id"], op=cmd["action"]["op"],
                    status=ack.get("status"), original_status=status, version=ack["version"], executed_at_ns=sampled)
        super().receive_ack(ack, now, source_node)

    def _recovery_ack(self, ack, cmd, now, node):
        tr = self.transaction
        original = tr["uncertain_command"]
        action, ttl = original["action"], self.config["observation_ttl_ns"]
        row = query_receipt(ack, cmd, node, now, self.work["work_id"], "recovery", ttl)
        if row is None or row["version"] < self.versions.get(node, 0):
            return
        self.versions[node] = row["version"]
        self.pending = None
        expiry = original["issued_at_ns"] + original["validity_ns"]
        expired_at_device = row["observed_at_ns"] > expiry
        row["matched"] = operation_matches(action, row["result"])
        self._event("device_readback", now, **row, reconciles_command_id=original["command_id"], op=action["op"])
        op = action["op"]
        state = {"cleanup": "cleaning", "abort": "aborting", "deactivate": "stopping"}.get(op, "deploying")
        tr["status"] = state
        if row["matched"] and (row["version"] >= original["new_version"] or expired_at_device):
            if op == "activate":
                self._activated(now, reconciled=True)
            elif op == "deactivate":
                self._deactivated(now, reconciled=True)
            else:
                if op in {"prepare", "install"} and node not in tr["touched"]:
                    tr["touched"].append(node)
                tr["index"] += 1
                self._next(now)
            return
        if not expired_at_device:
            tr.update(status="unknown", resume_at_ns=max(now + self.config["query_interval_ns"], expiry + 1))
            self._event("budget_hold", now, reason="original_write_may_still_arrive", service_id=action["service_id"],
                        command_id=original["command_id"], retained_wire_bps=self.budgets())
            return
        if op in {"prepare", "install", "activate"} and (
                now >= tr["deadline_ns"] or not self._eligible(action["service_id"], tr["decision"]["demand_revision"], now)):
            self._abort(now, "deployment_expired_or_demand_changed")
            return
        local_source = row["result"]["source"]
        if op in {"cleanup", "abort"} and local_source and local_source["enabled"] and local_source["active_epoch"] == tr["epoch"]:
            tr["rollback"] = op == "abort" or tr.get("rollback", False)
            tr["old"] = tr.get("old") or {"epoch": tr["epoch"], "decision": tr["decision"]}
            tr["status"] = "stopping"
            self._issue(now, node, {"op": "deactivate", "service_id": action["service_id"], "epoch": tr["epoch"]})
            return
        self._issue(now, node, deepcopy(action))


def install(sim, roles, emit, *, defer_unreachable_cleanup=False):
    """Bind the work order through delivered caches and the single packet command channel."""
    if hasattr(sim, "connect_work_order"):
        return sim.connect_work_order(roles, emit, defer_unreachable_cleanup=defer_unreachable_cleanup)
    from resnet_bridge import bind_work_loop
    return bind_work_loop(sim, roles, emit, defer_unreachable_cleanup=defer_unreachable_cleanup)
