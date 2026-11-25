"""Reconstruct rule inputs and final device decisions using sealed observations and host contracts."""
from pathlib import Path
from types import SimpleNamespace
import sys


def host_roles():
    # The bundled packet package supplies the standard-library QoS arithmetic.
    # Importing the role helpers starts neither an engine nor an A2A server.
    from scene import ROOT
    packet = str(ROOT / "src" / "packet")
    bundled = str(ROOT / "simulator" / "resnet_py" / "src")
    for directory in (packet, bundled):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    import packet_roles
    return packet_roles


def from_observation(conditions, state, expected_diagnosis, plan, request, host_config):
    roles = host_roles()
    grant = conditions["authority"]
    allowed = None if grant["discovery"] == "all_delivered_registered" else grant["allowedServiceIds"]
    waterline = conditions["simulation"]["control"]["control_headroom_fraction"]
    agents = roles._PacketAgents(allowed, grant["protectedServiceIds"], waterline,
                                 SimpleNamespace(identity=conditions["rcPolicy"]))
    allowed, protected = agents.scopes(state)
    snapshot, ledger = roles._model(state, waterline, allowed - protected)
    config = roles._configuration(protected, snapshot)
    diagnosis = roles._diagnose(state, snapshot, ledger, config, allowed, protected)
    diagnosis["authority_mode"] = "all_arrived_registered" if agents.allowed is None else "explicit_whitelist"
    fields = {k:expected_diagnosis.get(k) for k in diagnosis}
    if (fields != diagnosis or request["authority"] != diagnosis["authority"] or request["snapshot"] != snapshot
            or host_config != config or plan["policy_hash"] != agents.policy_hash
            or plan["authorized"] != diagnosis["authority"]["executableServiceIds"]
            or state["now_ns"] != plan["time_ns"]):
        raise ValueError("Rule input differs from the sealed OM observation, grant, host config or policy")
    return roles, snapshot, config


def verify_final(conditions, plan, state, request, roles, snapshot, config, target):
    from rule_patch import check_final
    from strong_tools import digest, validate_proposal
    audit = plan["solver_audit"]
    authority = request["authority"]
    action = request["action"]
    first = audit.get("openingAdmission")
    if first is not None:
        from opening import check_opening
        hold = action["tool"] == "hold_current" or action["scope"]["kind"] in ("retain","current")
        target, admission = check_opening(target, snapshot, config, authority,
                                          conditions["rcPolicy"]["initialAdmission"], hold, rule_request=request)
        if first != admission:
            raise ValueError("Opening supplement did not follow the sealed action and output budgets")
    expected = {}
    rejected = []
    for sid in plan["authorized"]:
        demand = state["services"][sid]
        inactive = not (demand["active"] and demand["registered"])
        reason = "reported_inactive" if inactive else "no_feasible_b03_candidate"
        choice = roles._choice(demand["service"], target[sid], state["links"], revision=demand["revision"], reason=reason)
        expected[sid] = choice
        if target[sid] is not None and not choice["accepted"] and target[sid] != snapshot["assignments"][sid]:
            rejected.append(sid)
            target[sid] = None
    if expected != plan["decisions"] or rejected != audit.get("strictPacketQosRejected",[]):
        raise ValueError("Final per-service decisions or rejection reasons differ from the replay")
    if digest(target) != audit["targetHash"] or not validate_proposal(snapshot,config,authority,target)["valid"]:
        raise ValueError("Final rule allocation could not be reconstructed")
    check_final(request,target)
    bound = {"snapshot":snapshot,"authority":authority}
    # The public span validator consumes the original common binding as well.
    bound.update(config=config,identity=audit["binding"])
    logical = roles.candidate_scope(bound,bound,target)
    physical = roles._device_scope(bound,state,target)
    changed = physical["changedServiceIds"]
    if (plan["solver_scope"] != logical or plan["scope"] != physical
            or plan["release_ids"] != [s for s in changed if s in state["confirmed"]]
            or plan["install_ids"] != [s for s in changed if target[s] is not None]):
        raise ValueError("Final rule patch differs from release or installation scope")
