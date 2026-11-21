"""Frozen RC tool choice over delivered packet evidence. All configuration writes stay in the work-order executor."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
import time

from model_component import load_model, validate_model_choice
from scene import PROFILE, ROOT

SCHEMA = "b03.packet.rc-choice.v1"
QOS = ("bandwidthBps", "delayMs", "jitterMs", "ber", "packetLossRate")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def indexed_table(columns, rows):
    """Lossless column-default encoding; bit j replaces default j in the row."""
    from collections import Counter
    defaults = []
    for j in range(1, len(columns)):
        frequency = Counter(canonical(row[j]) for row in rows)
        defaults.append(json.loads(min(frequency, key=lambda v: (-frequency[v], v))) if frequency else None)
    packed = []
    for row in rows:
        mask, values = 0, []
        for j, value in enumerate(row[1:]):
            if canonical(value) != canonical(defaults[j]):
                mask |= 1 << j
                values.append(value)
        packed.append([row[0], mask, *values])
    return {"columns": columns, "defaults": defaults, "rows": packed}


def policy_observation(state, snapshot, ledger, diagnosis, config, actions):
    """Project the OM/RC snapshot. The raw per-endpoint clocks remain in the work-order evidence."""
    nodes = {name: i for i, name in enumerate(sorted(snapshot["nodes"]))}
    links = {name: i for i, name in enumerate(sorted(snapshot["links"]))}
    services = {name: i for i, name in enumerate(sorted(diagnosis["services"]))}
    link_rows = []
    for name, i in links.items():
        link = snapshot["links"][name]
        status = state["links"][name]
        link_rows.append([i, nodes[link["srcNodeId"]], nodes[link["dstNodeId"]], status.get("reason"),
                          link["_online"], *[link["params"][key] for key in QOS],
                          status.get("capacity_bps"), ledger["reported_wire_bps"].get(name, 0)])
    svc_rows, route_rows = [], []
    for name, i in services.items():
        status = diagnosis["services"][name]
        demand = state["services"].get(name, {})
        spec = demand.get("service", {})
        service = [nodes.get(spec.get("src_node_id")), nodes.get(spec.get("dst_node_id")),
                   spec.get("service_type"), spec.get("category"), spec.get("priority"),
                   spec.get("required_bps"), spec.get("delay_limit_s"), spec.get("jitter_limit_s"),
                   spec.get("ber_limit"), spec.get("loss_limit")]
        svc_rows.append([i, *service, demand.get("fresh"), demand.get("active"), demand.get("revision"),
                         status["status"], status["authorized"], status["protected"]])
        rec = state["confirmed"].get(name)
        if rec:
            old = rec["decision"]
            route_rows.append([i, [links[lid] for lid in old["link_path"]], old["reserved_bps"],
                               old["wire_bps"], rec["epoch"], rec["configuration_evidence"]["verified"]])
    scopes = []
    catalog = {}
    for action in actions:
        service_ids = sorted(services[k] for k in action["scope"]["serviceIds"] if k in services)
        if service_ids not in scopes:
            scopes.append(service_ids)
        catalog[action["id"]] = {"tool": action["tool"], "scope": action["scope"]["kind"],
                                 "serviceSet": scopes.index(service_ids), "pool": action["pool"],
                                 "budget": action["budget"], "stage": action["stage"]}
    return {"schema": SCHEMA, "timeNs": state["now_ns"], "legalActions": list(catalog),
            "actionCatalog": catalog, "authorizedServiceSets": scopes,
            "nodes": indexed_table(["id","online"], [[i,snapshot["nodes"][name]["_online"]] for name,i in nodes.items()]),
            "links": indexed_table(["id","src","dst","observedStatus","usable","availableWireBps","delayMs","jitterMs",
                                    "ber","loss","observedCapacityBps","heldWireBps"], link_rows),
            "services": indexed_table(["id","src","dst","type","category","priority","payloadBps","maxDelayS","maxJitterS",
                                       "maxBer","maxLoss","fresh","active","revision","diagnosis","writable","protected"],svc_rows),
            "configs": indexed_table(["service","links","payloadBps","wireBps","epoch","verified"],route_rows),
            "budgets": {k:config[k] for k in ("path_limit","path_expansion_budget","search_budget","search_service_limit")}}


class FrozenActor:
    def __init__(self, checkpoint, tokenizer_path, device):
        import torch
        from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
        spec = json.loads((ROOT / "config/model.json").read_bytes())
        self.torch = torch
        self.device = torch.device(device)
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("Requested CUDA is unavailable")
        weights = Path(checkpoint).expanduser().resolve(strict=True)
        tokenizer_dir = Path(tokenizer_path).expanduser().resolve(strict=True) if tokenizer_path else weights
        for name, expected in spec["tokenizerSha256"].items():
            if file_digest(tokenizer_dir/name) != expected:
                raise ValueError("Tokenizer digest differs: "+name)
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir,local_files_only=True,trust_remote_code=False)
        config = AutoConfig.from_pretrained(tokenizer_dir,local_files_only=True,trust_remote_code=False)
        if any(getattr(config,k) != value for k,value in spec["architecture"].items()):
            raise ValueError("Checkpoint architecture differs from the declared Qwen model")
        if spec["format"] == "hf-safetensors":
            for name, expected in spec["weightFilesSha256"].items():
                if file_digest(weights/name) != expected:
                    raise ValueError("Checkpoint digest differs: "+name)
            self.version = spec["version"]
            self.model = AutoModelForCausalLM.from_pretrained(weights,local_files_only=True,trust_remote_code=False,
                torch_dtype=torch.float32,use_safetensors=True,attn_implementation="sdpa")
        else:
            metadata = json.loads((weights/"metadata.json").read_bytes())
            self.version = metadata["parameterVersion"]
            expected = spec["nativeVersions"].get(str(self.version))
            if not expected or file_digest(weights/"training.pt") != expected["checkpointSha256"] or file_digest(weights/"metadata.json") != expected["metadataSha256"]:
                raise ValueError("Expected a pinned native checkpoint and matching metadata")
            state = torch.load(weights/"training.pt",map_location="cpu",mmap=True,weights_only=True)
            with torch.device(self.device):
                self.model = AutoModelForCausalLM.from_config(config,torch_dtype=torch.float32,attn_implementation="sdpa")
            self.model.load_state_dict(state["parameters"],strict=True)
            self.model.tie_weights()
            del state
        self.model.to(self.device).eval().requires_grad_(False)
        self.symbols = spec["symbols"]
        self.token_ids = []
        for symbol in self.symbols:
            ids = self.tokenizer.encode(symbol,add_special_tokens=False)
            if len(ids) != 1:
                raise ValueError("Expected one token for action "+symbol)
            self.token_ids.append(ids[0])
        self.limit = min(spec["tokenLimit"],int(config.max_position_embeddings))
        self.instructions = ("Choose one legal resource-control tool from the current delivered packet observation. "
            +spec["actionDescriptions"]+" Each table row is [id,bitmask,exceptions]. Bit j (zero-based) replaces defaults[j] for columns[j+1], in increasing bit order. "
            "Packet rates and shared wire budgets are separate. Preserve original QoS, priority, write permission and healthy paths. "
            "Reply with one legal symbol.")
        self.identity = {"kind":"frozen-model","version":self.version,"schema":SCHEMA,"symbols":self.symbols,
                         "modelSpecSha256": file_digest(ROOT/"config/model.json"),
                         "checkpointFilesSha256": spec["weightFilesSha256"] if spec["format"]=="hf-safetensors" else
                         {"training.pt": expected["checkpointSha256"],"metadata.json":expected["metadataSha256"]},
                         "selection":"greedy_legal_token","precision":"fp32-bf16-autocast" if self.device.type=="cuda" else "fp32"}

    def choose(self, observation):
        torch = self.torch
        messages = [{"role":"system","content":self.instructions},{"role":"user","content":canonical(observation)}]
        tokens = self.tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True)
        if len(tokens) > self.limit:
            raise ValueError(f"Complete packet input has {len(tokens)} tokens; this profile accepts {self.limit}.")
        with torch.inference_mode(), torch.autocast(device_type=self.device.type,dtype=torch.bfloat16,enabled=self.device.type=="cuda"):
            x=torch.tensor([tokens],device=self.device)
            logits=self.model(input_ids=x,attention_mask=torch.ones_like(x),use_cache=False,logits_to_keep=1).logits[0,-1]
            allowed=[i for i,s in enumerate(self.symbols) if s in observation["legalActions"]]
            scores=logits[[self.token_ids[i] for i in allowed]].float().softmax(-1).cpu().tolist()
        if not scores or not all(math.isfinite(p) for p in scores):
            raise ValueError("No finite legal action scores")
        choices=[self.symbols[i] for i in allowed]
        action=choices[max(range(len(scores)),key=scores.__getitem__)]
        return action,{"inputTokens":len(tokens),"inputSha256":hashlib.sha256(canonical(messages).encode()).hexdigest(),
                       "legalProbabilities":dict(zip(choices,scores)),"behaviorProbability":1.0,
                       "parameterVersion":self.version,"observation":observation}


class DecisionPolicy:
    def __init__(self, name, checkpoint=None, tokenizer=None, device="cuda", initial_admission="residual",
                 components=None):
        self.opening_checked = False
        self.initial_admission = initial_admission
        self.name = name
        self.context = {}
        self.components = components
        self.record_component_io = False
        self.actor = None
        self._last_frozen_version = None
        identity = {"kind": "rule", "method": name}
        if name == "model":
            if components is None:
                if not checkpoint:
                    raise ValueError("--policy model requires --checkpoint DIRECTORY")
                self.actor = FrozenActor(checkpoint, tokenizer, device)
                identity = self.actor.identity
            else:
                self.actor, identity = load_model(components, checkpoint, tokenizer, device, FrozenActor)
        self.identity = dict(identity, initialAdmission=initial_admission)

    def _model_choice(self, by_id, state, snapshot, ledger, diagnosis, config):
        symbols = self.identity.get("symbols")
        if symbols is None:
            symbols = self.actor.symbols
        if self.components is not None and by_id.keys() - set(self.components.symbols):
            raise ValueError("The enumerated actions contain an unregistered catalog tool")
        legal = [by_id[s] for s in symbols if s in by_id]
        if not legal:
            raise ValueError("The model has no legal tool under the current authorization")
        observation = policy_observation(state, snapshot, ledger, diagnosis, config, legal)
        current_version = getattr(self.actor, "version", None)
        result = self.actor.choose(deepcopy(observation))
        if not isinstance(result, (tuple, list)) or len(result) != 2 or not isinstance(result[1], dict):
            raise ValueError("Model choose must return (tool_id, details)")
        selected, details = result
        if not isinstance(selected, str) or selected not in observation["legalActions"]:
            raise ValueError("Model selected a tool outside its delivered legal actions")
        reserved = {"method", "modelCalled", "selected", "selectionWallNs", "adapterId", "adapterVersion",
                    "catalogVersion", "entrypoint", "mode", "modelIO", "legalActions", "actionCatalog"}
        conflict = reserved.intersection(details)
        if conflict:
            raise ValueError(f"Model details contain runner-owned fields: {sorted(conflict)}")
        if "observation" in details and canonical(details["observation"]) != canonical(observation):
            raise ValueError("Model details contain a different observation from the delivered input")
        diagnostics = {key: value for key, value in details.items() if key != "observation"}
        if not all(isinstance(key, str) for key in diagnostics):
            raise ValueError("Model diagnostic keys must be strings")
        try:
            receipt = json.loads(canonical(diagnostics))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Model diagnostics must be finite JSON values") from exc
        receipt.update(method=self.name, modelCalled=True, selected=selected, observation=observation)
        for key in ("adapterId", "adapterVersion", "catalogVersion"):
            if key in self.identity:
                receipt[key] = self.identity[key]
        if "policyVersion" not in receipt and "parameterVersion" in receipt:
            receipt["policyVersion"] = receipt["parameterVersion"]
        validate_model_choice(receipt, self.identity)
        version = receipt["policyVersion"]
        if current_version is not None and (type(version) is not type(current_version) or version != current_version):
            raise ValueError("Model choice version differs from the actor at selection")
        if self.identity.get("mode", "frozen") == "frozen":
            if self._last_frozen_version is not None and version != self._last_frozen_version:
                raise ValueError("Frozen policy version changed during the run")
            self._last_frozen_version = version
        return selected, receipt

    def select(self, actions, state, snapshot, ledger, diagnosis, config):
        at = time.perf_counter_ns()
        by_id = {}
        for action in actions:
            symbol = action.get("id") if isinstance(action, dict) else None
            if not isinstance(symbol, str) or not symbol.strip() or symbol in by_id:
                raise ValueError("Enumerated actions require distinct tool IDs")
            by_id[symbol] = action
        if self.actor is not None:
            choice, receipt = self._model_choice(by_id, state, snapshot, ledger, diagnosis, config)
        else:
            if self.name == "combined":
                affected = sum(d["authorized"] for d in diagnosis["services"].values())
                choice = "D" if affected <= 2 else "K" if affected <= 8 else "J"
            else:
                choice = self.name
            if choice not in by_id:
                raise ValueError(f"Requested tool {choice} is unavailable under the current authorization")
            receipt = {"method": self.name, "modelCalled": False, "selected": choice}
        receipt["selectionWallNs"] = time.perf_counter_ns() - at
        return by_id[choice], receipt
