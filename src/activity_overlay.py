"""Apply explicitly scheduled demand ends to the native packet traffic batches."""
from dataclasses import replace


def parse_service_ends(values, specs, selected, start, end):
    ends = {}
    for value in values:
        sid, sep, raw = value.partition("=")
        if not sep or sid not in selected or sid in ends:
            raise ValueError("Expected a unique selected service ID followed by =SECONDS")
        try:
            second = int(raw)
        except ValueError as exc:
            raise ValueError("Service end must be an integer simulation second") from exc
        if not start < second < end or second * 1_000_000 <= specs[sid].registered_at_us:
            raise ValueError("Service end must follow registration and fall inside the scored window")
        ends[sid] = second
    return ends


def with_service_ends(scenario, ends):
    """Use a native traffic_set. Demand reports, reservations, commands and queues are handled by the simulator."""
    if not ends:
        return scenario
    deadlines = {sid: sec * 1_000_000 for sid, sec in ends.items()}
    batches = {b.time_us: b for b in scenario.batches}
    times = sorted(set(batches) | set(deadlines.values()))
    services = (*scenario.services, *scenario.added_services)
    previous = set()
    native_active = set()
    profile = None
    result = []
    for sequence, at in enumerate(times):
        batch = batches.get(at)
        operations = []
        native_boundary = False
        if batch is not None:
            native_active = set(batch.active_service_ids)
            profile = batch.profile
            native_boundary = any(op["kind"] == "traffic_set" for op in batch.operations)
            operations = [op for op in batch.operations if op["kind"] != "traffic_set"]
        active = native_active - {sid for sid, cutoff in deadlines.items() if cutoff <= at}
        if native_boundary or at in deadlines.values():
            registered = {s.service_id for s in services if s.registered_at_us <= at}
            operations.append({"kind": "traffic_set", "application_mode": "replace_allowlist",
                               "activate_service_ids": sorted(active - previous),
                               "stop_new_packets_service_ids": sorted(registered - active),
                               "active_service_ids": sorted(active), "queue_rule": "do_not_clear_existing_queues"})
        result.append(replace(batch or scenario.batches[0], time_us=at, sequence=sequence,
                              operations=tuple(operations), profile=profile, active_service_ids=tuple(sorted(active))))
        previous = active
    return replace(scenario, batches=tuple(result))
