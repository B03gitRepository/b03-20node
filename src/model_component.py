"""Load a model adapter and verify a model decision from its saved packet receipt."""

from __future__ import annotations

import math
import re
from copy import deepcopy


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _version(value):
    return _text(value) or (type(value) is int and value >= 0)


def _symbols(values, label):
    if (not isinstance(values, (tuple, list)) or not values
            or not all(_text(s) for s in values) or len(set(values)) != len(values)):
        raise ValueError(f"{label} must be an ordered list of distinct tool IDs")
    return list(values)


def load_model(components, checkpoint, tokenizer, device, frozen_actor):
    """Return the actor and the identity to put in CONDITIONS.json."""
    cfg = deepcopy(components.model)
    if not isinstance(cfg, dict) or not all(_text(cfg.get(k)) for k in ("id", "entrypoint", "catalogVersion")):
        raise ValueError("Model registration requires id, entrypoint and catalogVersion")
    if not _version(cfg.get("version")) or cfg.get("mode") not in ("frozen", "online-ppo"):
        raise ValueError("Invalid adapter version or model mode")
    symbols = _symbols(cfg.get("symbols"), "model.symbols")
    catalog = _symbols(components.symbols, "catalog.symbols")
    if not set(symbols) <= set(catalog):
        raise ValueError("Model tool IDs must belong to the registered catalog")
    if cfg["entrypoint"] == "builtin":
        if cfg["mode"] != "frozen" or not checkpoint:
            raise ValueError("The built-in frozen actor requires frozen mode and --checkpoint DIRECTORY")
        actor = frozen_actor(checkpoint, tokenizer, device)
    else:
        factory = components.factory("model")
        if not callable(factory):
            raise ValueError("Model entrypoint must resolve to a factory")
        actor = factory(checkpoint=checkpoint, tokenizer=tokenizer, device=device, config=deepcopy(cfg))
    if not callable(getattr(actor, "choose", None)):
        raise ValueError("Model adapter must implement choose(observation)")
    if cfg["mode"] == "online-ppo":
        if not all(callable(getattr(actor, name, None)) for name in ("save_checkpoint", "restore_checkpoint")):
            raise ValueError("Online PPO requires save_checkpoint(path) and restore_checkpoint(path)")
        if checkpoint is not None:
            actor.restore_checkpoint(checkpoint)
    identity = deepcopy(getattr(actor, "identity", {}))
    if not isinstance(identity, dict):
        raise ValueError("Actor identity must be a mapping")
    for declared in (getattr(actor, "symbols", symbols), identity.get("symbols", symbols)):
        if _symbols(declared, "actor symbols") != symbols:
            raise ValueError("Actor tool mapping differs from model.symbols")
    native = cfg["entrypoint"] == "builtin" or identity.get("kind") == "frozen-model"
    if native and (cfg["mode"] != "frozen" or identity.get("selection") != "greedy_legal_token"
                   or not _version(identity.get("version"))):
        raise ValueError("The native frozen identity requires its pinned version and greedy selection")
    declared_versions = [identity[key] for key in ("policyVersion", "version") if key in identity]
    if hasattr(actor, "version"):
        declared_versions.append(actor.version)
    if declared_versions:
        expected = declared_versions[0]
        if not _version(expected) or any(not _version(v) or v != expected for v in declared_versions):
            raise ValueError("Actor declares invalid or conflicting policy versions")
        if cfg["mode"] == "frozen":
            identity["policyVersion"] = expected
        else:
            identity.pop("policyVersion", None)
            identity["initialPolicyVersion"] = expected
    identity.update(kind="frozen-model" if native else "model-adapter", symbols=symbols,
                    adapterId=cfg["id"], adapterVersion=cfg["version"], entrypoint=cfg["entrypoint"],
                    catalogVersion=cfg["catalogVersion"], mode=cfg["mode"])
    return actor, identity


def _probability(value):
    return type(value) in (float, int) and 0 <= value <= 1


def _distribution(value, legal, name):
    if (not isinstance(value, dict) or set(value) != set(legal)
            or not all(_probability(p) for p in value.values())
            or abs(math.fsum(value.values()) - 1.0) >= 1e-5):
        raise ValueError(f"{name} must be a finite probability distribution over the legal tools")
    return value


def validate_model_choice(choice, policy_identity):
    """Return True for an auditable model tool_choice, otherwise raise ValueError.

    policy_identity is CONDITIONS.rcPolicy. Historic native receipts use parameterVersion.
    This function does not load a checkpoint or call an adapter.
    """
    if not isinstance(choice, dict) or not isinstance(policy_identity, dict):
        raise ValueError("Expected a tool choice and a policy identity")
    if policy_identity.get("kind") not in ("frozen-model", "model-adapter", "t132-packet-policy"):
        raise ValueError("Expected an active model policy identity")
    if choice.get("method") != "model" or choice.get("modelCalled") is not True:
        raise ValueError("Expected a recorded model invocation")
    if "selectionWallNs" in choice and (type(choice["selectionWallNs"]) is not int or choice["selectionWallNs"] < 0):
        raise ValueError("Invalid model selection wall time")
    registered_adapter = policy_identity.get("kind") == "model-adapter" or "entrypoint" in policy_identity
    if registered_adapter:
        if not all(_text(policy_identity.get(k)) for k in ("adapterId", "catalogVersion", "entrypoint")):
            raise ValueError("Registered model identity is incomplete")
        if not _version(policy_identity.get("adapterVersion")) or "mode" not in policy_identity:
            raise ValueError("Registered model identity is missing its adapter version or mode")
        for key in ("adapterId", "adapterVersion", "catalogVersion"):
            if (key not in choice or type(choice[key]) is not type(policy_identity[key])
                    or choice[key] != policy_identity[key]):
                raise ValueError(f"Model choice differs from the registered {key}")
    obs = choice.get("observation")
    if not isinstance(obs, dict):
        raise ValueError("The delivered model observation is missing")
    if "schema" in policy_identity and obs.get("schema") != policy_identity["schema"]:
        raise ValueError("Delivered observation schema differs from the model identity")
    legal = _symbols(obs.get("legalActions"), "observation.legalActions")
    catalog = obs.get("actionCatalog")
    if not isinstance(catalog, dict) or set(catalog) != set(legal):
        raise ValueError("Legal tool IDs and actionCatalog do not agree")
    sets = obs.get("authorizedServiceSets")
    for entry in catalog.values():
        if (not isinstance(entry, dict) or not {"tool", "scope", "serviceSet", "pool", "budget", "stage"} <= entry.keys()
                or not isinstance(sets, list) or type(entry["serviceSet"]) is not int
                or not 0 <= entry["serviceSet"] < len(sets)):
            raise ValueError("Missing tool meaning or invalid service-set reference in actionCatalog")
    registered = _symbols(policy_identity.get("symbols"), "policy symbols")
    if legal != [s for s in registered if s in legal]:
        raise ValueError("Observation contains unknown or out-of-order model tool IDs")
    selected = choice.get("selected")
    if not _text(selected) or selected not in legal or choice.get("action", selected) != selected:
        raise ValueError("Model selected a tool outside its delivered legal actions or reported a different action")
    version = choice.get("policyVersion", choice.get("parameterVersion"))
    if not _version(version):
        raise ValueError("A model choice requires a non-empty policy version")
    if "parameterVersion" in choice and (not _version(choice["parameterVersion"]) or choice["parameterVersion"] != version):
        raise ValueError("Model policy and parameter versions differ")
    native = policy_identity.get("kind") == "frozen-model"
    mode = policy_identity.get("mode", "frozen")
    if mode not in ("frozen", "online-ppo") or (native and mode != "frozen"):
        raise ValueError("Invalid model identity mode")
    if mode == "frozen":
        for name in ("version", "policyVersion"):
            if name in policy_identity and (not _version(policy_identity[name]) or version != policy_identity[name]):
                raise ValueError("Choice version differs from the frozen policy identity")
    if "inputTokens" in choice and (type(choice["inputTokens"]) is not int or choice["inputTokens"] <= 0):
        raise ValueError("Invalid model input token count")
    if "inputSha256" in choice and not (_text(choice["inputSha256"]) and re.fullmatch(r"[a-f0-9]{64}", choice["inputSha256"])):
        raise ValueError("Invalid model input SHA-256")
    if "legalProbabilities" in choice:
        _distribution(choice["legalProbabilities"], legal, "legalProbabilities")
    if "behaviorProbability" in choice:
        p = choice["behaviorProbability"]
        if not _probability(p) or p <= 0:
            raise ValueError("The selected behavior probability must be positive and finite")
    if "behaviorProbabilities" in choice:
        behavior = _distribution(choice["behaviorProbabilities"], legal, "behaviorProbabilities")
        if ("behaviorProbability" not in choice
                or not math.isclose(choice["behaviorProbability"], behavior[selected], rel_tol=1e-6, abs_tol=0)):
            raise ValueError("Selected behavior probability differs from the behavior distribution")
    if "oldLogProbability" in choice:
        logp = choice["oldLogProbability"]
        if (type(logp) not in (float, int) or "behaviorProbability" not in choice
                or not math.isclose(logp, math.log(choice["behaviorProbability"]), rel_tol=1e-6, abs_tol=1e-6)):
            raise ValueError("Old log probability differs from the selected behavior probability")
    if native:
        required = {"inputTokens", "inputSha256", "parameterVersion", "legalProbabilities", "behaviorProbability"}
        if not required <= choice.keys() or "version" not in policy_identity:
            raise ValueError("Native frozen choice is missing its original inference diagnostics")
        probs = choice["legalProbabilities"]
        if choice["behaviorProbability"] != 1 or probs[selected] != max(probs.values()):
            raise ValueError("Native frozen actor must select a greedy legal token")
        if "behaviorProbabilities" in choice and any(
                p != int(s == selected) for s, p in choice["behaviorProbabilities"].items()):
            raise ValueError("Native greedy behavior distribution must assign all mass to the selected tool")
    if mode == "online-ppo":
        if (not {"behaviorProbabilities", "behaviorProbability"} <= choice.keys()
                or choice.get("policyMode", "sample") != "sample"):
            raise ValueError("Online PPO requires the actual sampling distribution and selected probability")
    return True
