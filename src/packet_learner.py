"""Full-parameter online PPO from the pinned 100-node v374 actor.

The replay journal stores actual observations, sampling probabilities and measured
unweighted packet returns. Each committed version reconstructs from the original
checkpoint by one deterministic optimizer step per recorded update.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import frozen_policy
from frozen_policy import FrozenActor, SCHEMA, canonical, file_digest


BASE = 374
JOURNAL_SCHEMA = "b03.packet.full-ppo-replay.v2"
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 16 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _fingerprints(model):
    """Merkle-style digest of every byte, tensor name, shape and dtype, including the backbone."""
    per_tensor = {}
    manifest = []
    for name, tensor in sorted(model.named_parameters()):
        value = tensor.detach().reshape(-1)
        block_size = max(1, HASH_CHUNK_BYTES // value.element_size())
        sha = hashlib.sha256()
        for block in value.split(block_size):
            array = block.cpu().contiguous().numpy()
            sha.update(memoryview(array).cast("B"))
        per_tensor[name] = sha.hexdigest()
        manifest.append([name, list(tensor.shape), str(tensor.dtype), per_tensor[name]])
    return _digest(manifest), per_tensor


def _uniform(seed, draw):
    key = f"b03.packet.sample.v1/{seed}/{draw}".encode("ascii")
    integer = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") >> 11
    return integer / (1 << 53)


def _decision_digest(decision):
    return _digest([decision[k] for k in ("sampleId", "sampleDraw", "inputSha256", "tokenIdsSha256",
                    "parameterSha256", "action", "legalProbabilities", "behaviorProbabilities",
                    "behaviorProbability", "oldLogProbability")])


def _sample_index(probabilities, uniform):
    threshold = uniform * math.fsum(probabilities)
    running = 0.0
    last_positive = None
    for index, probability in enumerate(probabilities):
        running += probability
        if probability > 0:
            last_positive = index
            if threshold < running:
                return index
    if last_positive is None:
        raise ValueError("No positive legal behavior probability")
    return last_positive


@contextmanager
def _journal_lock(path):
    """Stable inode across atomic journal replacement; the OS releases it on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _load(path):
    if path.stat().st_size > MAX_JOURNAL_BYTES:
        raise ValueError("Packet PPO replay journal exceeds its byte limit")
    with path.open("rb") as stream:
        envelope = json.load(stream)
    body = envelope["journal"]
    if envelope["sha256"] != _digest(body) or body["schema"] != JOURNAL_SCHEMA:
        raise ValueError("Corrupt packet PPO replay journal")
    return body, envelope["sha256"]


def _save(path, body):
    checksum = _digest(body)
    raw = canonical({"journal": body, "sha256": checksum}).encode("utf-8")
    if len(raw) > MAX_JOURNAL_BYTES:
        raise ValueError("Packet PPO replay journal exceeds its byte limit")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return checksum


def unweighted_advantage(credit):
    if credit.get("script") not in ("04", "05") or credit.get("nodes") != 100:
        raise ValueError("Only registered 100-node scripts 04 and 05 supply training credit")
    for key in ("fromNs", "toNs", "actualUnmetServiceNs", "fixedIUnmetServiceNs"):
        if type(credit.get(key)) is not int or credit[key] < 0:
            raise ValueError("Unweighted packet credit must use measured nonnegative integers")
    if credit["fromNs"] >= credit["toNs"]:
        raise ValueError("Credit must follow the sampled decision within the scored window")
    raw = -credit["actualUnmetServiceNs"] / 1e9
    control = -credit["fixedIUnmetServiceNs"] / 1e9
    if credit.get("rawReturnSeconds") != raw or credit.get("fixedIReturnSeconds") != control:
        raise ValueError("Stored U returns differ from the measured service clock")
    return (credit["fixedIUnmetServiceNs"] - credit["actualUnmetServiceNs"]) / 1e9


class PacketLearner(FrozenActor):
    """A DecisionPolicy actor with explicit sampled and greedy modes."""

    def __init__(self, checkpoint, tokenizer, device="cuda", *, seed=13201,
                 exploration=0.25, lr=1e-3, clip=0.2, grad_clip=1.0, max_updates=16):
        import torch
        import transformers

        if type(seed) is not int or not 0 <= seed < 2**63:
            raise ValueError("seed must be an integer in [0, 2**63)")
        if type(max_updates) is not int or max_updates < 1:
            raise ValueError("max_updates must be a positive integer")
        if not (0 <= exploration < 1 and 0 < clip < 1 and lr > 0 and grad_clip > 0
                and all(math.isfinite(x) for x in (exploration, lr, clip, grad_clip))):
            raise ValueError("Invalid exploration or PPO/SGD configuration")
        target = torch.device(device)
        if target.type not in ("cuda", "cpu"):
            raise ValueError("Expected a CUDA or CPU device")
        if target.type == "cuda":
            workspace = os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
            if workspace not in (":4096:8", ":16:8"):
                raise ValueError("Deterministic cuBLAS needs :4096:8 or :16:8 before CUDA initialization")
            if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
                raise ValueError("Packet training requires a CUDA device with BF16 support")
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
            torch.backends.cudnn.benchmark = False
        else:
            torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        torch.manual_seed(seed)
        super().__init__(checkpoint, tokenizer, device)
        if self.version != BASE or list(self.symbols) != list("ABCDEFGHIJKL"):
            raise ValueError("Expected the released 100-node v374 actor with its twelve symbols")
        if self.model.config.attention_dropout or any(
                isinstance(m, torch.nn.Dropout) and m.p for m in self.model.modules()):
            raise ValueError("Checkpoint replay requires the pinned zero-dropout architecture")
        self.model.config.use_cache = False
        self.model.requires_grad_(True)
        self.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        self.parameters = tuple(self.model.parameters())
        if not self.parameters or any(p.dtype != torch.float32 or not p.requires_grad for p in self.parameters):
            raise ValueError("Every native model parameter must be trainable FP32")
        self.optimizer = torch.optim.SGD(self.parameters, lr=lr, momentum=0.0,
                                         weight_decay=0.0, foreach=False)
        self.algorithm = {
            "baseVersion": BASE, "optimizer": "full-parameter SGD, zero momentum and weight decay",
            "lr": float(lr), "clip": float(clip), "gradClip": float(grad_clip),
            "exploration": float(exploration), "seed": seed, "maxUpdates": max_updates,
            "sampleRng": "b03.packet.sample.v1 SHA256-counter top53 inverse CDF",
            "fingerprint": "SHA256 of sorted [name, shape, dtype, SHA256(all tensor bytes)] manifest",
            "loss": "mean clipped PPO on selected legal mixture log-probability",
            "return": "negative future unweighted unmet service-seconds from the packet meter",
            "reward": "actual return minus the matched fixed-I return, measured after each decision",
            "baseline": "matched future fixed-I return", "advantageNormalization": False, "epochs": 1,
            "checkpointing": "non-reentrant", "cpuActivationOffload": target.type == "cuda",
        }
        self.runtime = {
            "torch": str(torch.__version__), "transformers": str(transformers.__version__),
            "device": target.type, "cuda": torch.version.cuda if target.type == "cuda" else None,
            "gpu": torch.cuda.get_device_name(target) if target.type == "cuda" else None,
            "capability": list(torch.cuda.get_device_capability(target)) if target.type == "cuda" else None,
            "cublasWorkspace": os.environ.get("CUBLAS_WORKSPACE_CONFIG") if target.type == "cuda" else None,
            "attention": "sdpa-flash" if target.type == "cuda" else "sdpa-cpu",
        }
        self.base_identity = dict(self.identity)
        self.parameter_sha256, self._tensor_hashes = _fingerprints(self.model)
        self._base_sha = self.parameter_sha256
        import packet_input
        self._source_hashes = {"learner": file_digest(Path(__file__)),
                               "packetPolicy": file_digest(Path(frozen_policy.__file__)),
                               "packetInput": file_digest(Path(packet_input.__file__))}
        self._base_directory = Path(checkpoint).expanduser().resolve(strict=True)
        self._resume_journal = None
        self._committed_path = None
        self._cursor = self._committed_cursor = 0
        self._draw_receipts = {}
        self._journal_hash = None
        self._dirty = False
        self.set_mode("sample")

    def _ready(self):
        if self._dirty:
            raise RuntimeError("Discard this partially committed learner and restore the last journal in a fresh actor")

    def set_mode(self, mode):
        self._ready()
        if mode not in ("sample", "greedy"):
            raise ValueError("Choose 'sample' for collection or 'greedy' for frozen evaluation")
        self.mode = mode
        self.model.train(mode == "sample")
        self.identity = dict(self.base_identity, kind="packet-full-ppo", baseVersion=BASE,
                             version=self.version, parameterSha256=self.parameter_sha256,
                             selection="sample_legal_mixture" if mode == "sample" else "greedy_legal_token",
                             algorithm=dict(self.algorithm))
        return self

    def _encode(self, observation):
        if observation.get("schema") != SCHEMA:
            raise ValueError("Expected the released full packet observation schema")
        provided = observation["legalActions"]
        allowed = [s for s in self.symbols if s in provided]
        if not allowed or len(provided) != len(allowed) or set(allowed) != set(observation["actionCatalog"]):
            raise ValueError("Unknown, duplicated, missing, or inconsistent legal packet tools")
        messages = [{"role": "system", "content": self.instructions},
                    {"role": "user", "content": canonical(observation)}]
        ids = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        if not ids:
            raise ValueError("The tokenizer returned an empty observation")
        proof = {}
        if len(ids) > self.limit:
            from packet_input import fit_complete_input
            ids, messages, proof = fit_complete_input(self, observation, ids, messages)
        return ids, allowed, _digest(messages), proof

    def _attention_kernel(self):
        if self.device.type != "cuda":
            return nullcontext()
        from torch.nn.attention import SDPBackend, sdpa_kernel
        return sdpa_kernel(SDPBackend.FLASH_ATTENTION)

    def _distribution(self, ids, allowed, grad):
        torch = self.torch
        cuda = self.device.type == "cuda"
        offload = torch.autograd.graph.save_on_cpu(pin_memory=True) if cuda and grad else nullcontext()
        x = torch.tensor([ids], device=self.device, dtype=torch.long)
        mask = torch.ones_like(x)
        columns = [self.token_ids[self.symbols.index(s)] for s in allowed]
        with torch.set_grad_enabled(grad), self._attention_kernel(), offload, torch.autocast(
                self.device.type, dtype=torch.bfloat16, enabled=cuda, cache_enabled=False):
            logits = self.model(input_ids=x, attention_mask=mask, use_cache=False,
                                logits_to_keep=1, return_dict=True).logits[0, -1, columns]
            # Twelve scalars at most. FP64 preserves the frozen categorical likelihood.
            log_pi = logits.double().log_softmax(0)
            epsilon = self.algorithm["exploration"]
            if epsilon:
                log_q = torch.logaddexp(log_pi + math.log1p(-epsilon),
                                        torch.full_like(log_pi, math.log(epsilon / len(allowed))))
                log_q = log_q - torch.logsumexp(log_q, 0)
            else:
                log_q = log_pi
        if not torch.isfinite(log_pi).all().item() or not torch.isfinite(log_q).all().item():
            raise ArithmeticError("Nonfinite legal action distribution")
        return log_pi, log_q

    def choose(self, observation):
        self._ready()
        ids, allowed, message_sha, proof = self._encode(observation)
        log_pi, log_q = self._distribution(ids, allowed, False)
        policy = log_pi.exp().tolist()
        if self.mode == "sample":
            draw = self._cursor
            behavior = log_q.exp().tolist()
            index = _sample_index(behavior, _uniform(self.algorithm["seed"], draw))
            old_logp = log_q[index].item()
            sample_id = f"{self.version}/{draw}"
            self._cursor += 1
        else:
            draw = sample_id = None
            index = max(range(len(policy)), key=policy.__getitem__)
            behavior = [float(i == index) for i in range(len(allowed))]
            old_logp = 0.0
        action = allowed[index]
        details = {
            "inputTokens": len(ids), "inputSha256": message_sha, "tokenIdsSha256": _digest(ids),
            "legalProbabilities": dict(zip(allowed, policy)),
            "behaviorProbabilities": dict(zip(allowed, behavior)),
            "behaviorProbability": behavior[index], "oldLogProbability": old_logp,
            "action": action, "sampleId": sample_id, "sampleDraw": draw, "policyMode": self.mode,
            "exploration": self.algorithm["exploration"] if self.mode == "sample" else 0.0,
            "probabilityFields": {"legalProbabilities": "pi=softmax of legal tool logits",
                                  "behaviorProbabilities": ("q=(1-epsilon)*pi+epsilon/legalCount"
                                                            if self.mode == "sample" else "q=one-hot greedy")},
            "samplingUniform": _uniform(self.algorithm["seed"], draw) if draw is not None else None,
            "parameterVersion": self.version, "parameterSha256": self.parameter_sha256,
            "observation": observation, **proof,
        }
        if self.mode == "sample":
            self._draw_receipts[draw] = {"draw": draw, "action": action, "allowed": allowed,
                                         "behavior": behavior, "receipt": _decision_digest(details)}
        return action, details

    def _header(self):
        return {"schema": JOURNAL_SCHEMA, "baseIdentity": self.base_identity, "baseSha256": self._base_sha,
                "sources": self._source_hashes, "algorithm": self.algorithm, "runtime": self.runtime}

    def _rows(self, samples):
        rows = []
        for decision, credit in samples:
            if (decision["policyMode"] != "sample" or decision["parameterVersion"] != self.version
                    or decision["parameterSha256"] != self.parameter_sha256):
                raise ValueError("Expected a genuine sampled decision at the current parameter version")
            receipt = self._draw_receipts.get(decision["sampleDraw"])
            if receipt is None or receipt["receipt"] != _decision_digest(decision):
                raise ValueError("The decision differs from the actor's original sampling receipt")
            ids, allowed, message_sha, proof = self._encode(decision["observation"])
            if (message_sha != decision["inputSha256"] or len(ids) != decision["inputTokens"]
                    or _digest(ids) != decision["tokenIdsSha256"]):
                raise ValueError("The retained decision does not reproduce its complete sampled input")
            action = decision["action"]
            if decision.get("selected", action) != action or decision.get("modelCalled", True) is not True:
                raise ValueError("The sampled and executed packet tools differ")
            rows.append({
                "ids": ids, "tokenIdsSha256": decision["tokenIdsSha256"], "inputSha256": message_sha,
                "observation": decision["observation"], "receipt": receipt["receipt"],
                "allowed": allowed, "action": action, "sampleId": decision["sampleId"],
                "draw": decision["sampleDraw"], "policy": [decision["legalProbabilities"][s] for s in allowed],
                "behavior": [decision["behaviorProbabilities"][s] for s in allowed],
                "oldProbability": decision["behaviorProbability"], "oldLogp": decision["oldLogProbability"],
                "credit": dict(credit), "rewardU": unweighted_advantage(credit), "encodingProof": proof,
            })
        return rows

    def _validate_rows(self, rows, draws, end_cursor, *, restore=False):
        if not rows or type(end_cursor) is not int or end_cursor <= self._committed_cursor:
            raise ValueError("Expected actual sampled decisions and a forward sampling cursor")
        if len(draws) != end_cursor - self._committed_cursor:
            raise ValueError("Sampling receipts do not cover the recorded cursor interval")
        for offset, receipt in enumerate(draws, start=self._committed_cursor):
            q, allowed = receipt["behavior"], receipt["allowed"]
            if (receipt["draw"] != offset or not allowed or len(q) != len(allowed)
                    or allowed != [s for s in self.symbols if s in allowed]
                    or not all(math.isfinite(v) and 0 <= v <= 1 for v in q)
                    or not math.isclose(math.fsum(q), 1, rel_tol=1e-12)
                    or receipt["action"] != allowed[_sample_index(q, _uniform(self.algorithm["seed"], offset))]):
                raise ValueError("A sampled action does not reproduce from its pre-selection cursor")
        seen = set()
        epsilon = self.algorithm["exploration"]
        for r in rows:
            draw, allowed, ids = r["draw"], r["allowed"], r["ids"]
            if (type(draw) is not int or not self._committed_cursor <= draw < end_cursor or draw in seen
                    or r["sampleId"] != f"{self.version}/{draw}"):
                raise ValueError("Duplicate, stale or invalid packet draw")
            seen.add(draw)
            receipt = draws[draw - self._committed_cursor]
            if (r["receipt"] != receipt["receipt"] or allowed != receipt["allowed"]
                    or r["behavior"] != receipt["behavior"] or r["action"] != receipt["action"]):
                raise ValueError("Credited action and original behavior draw disagree")
            if restore:
                rebuilt_ids, rebuilt_allowed, message_sha, proof = self._encode(r["observation"])
                if (rebuilt_ids != ids or rebuilt_allowed != allowed or message_sha != r["inputSha256"]
                        or proof != r["encodingProof"]):
                    raise ValueError("Archived packet message does not reproduce its original full model input")
            if not allowed or allowed != [s for s in self.symbols if s in allowed] or r["action"] not in allowed:
                raise ValueError("Invalid recorded legal mask or action")
            if (not 0 < len(ids) <= self.limit or any(type(i) is not int or not 0 <= i < self.model.config.vocab_size for i in ids)
                    or r["tokenIdsSha256"] != _digest(ids)):
                raise ValueError("Invalid or incomplete recorded token input")
            pi, q = r["policy"], r["behavior"]
            if (len(q) != len(allowed) or len(pi) != len(allowed)
                    or not all(math.isfinite(v) and 0 <= v <= 1 for v in [*pi, *q])
                    or not math.isclose(math.fsum(pi), 1.0, rel_tol=1e-12)
                    or not math.isclose(math.fsum(q), 1.0, rel_tol=1e-12)):
                raise ValueError("Invalid stored legal probabilities")
            if any(not math.isclose(b, (1 - epsilon) * p + epsilon / len(q), rel_tol=1e-12, abs_tol=1e-15)
                   for p, b in zip(pi, q)):
                raise ValueError("Recorded behavior is inconsistent with the exploration mixture")
            index = allowed.index(r["action"])
            if (index != _sample_index(q, _uniform(self.algorithm["seed"], draw))
                    or q[index] <= 0 or r["oldProbability"] != q[index]
                    or not math.isclose(math.log(q[index]), r["oldLogp"], abs_tol=1e-12)
                    or r["credit"]["fromNs"] != r["observation"]["timeNs"]
                    or r["rewardU"] != unweighted_advantage(r["credit"])):
                raise ValueError("Inconsistent sampled action, frozen log-probability or U reward")

    def _optimize(self, rows):
        torch = self.torch
        before, hashes = _fingerprints(self.model)
        if before != self.parameter_sha256 or hashes != self._tensor_hashes:
            raise ValueError("Live parameters differ from the recorded rollout parent")
        self.model.train()
        self.optimizer.zero_grad(set_to_none=True)
        ratios, losses, score_errors = [], [], []
        try:
            for row in rows:
                _, log_q = self._distribution(row["ids"], row["allowed"], True)
                index = row["allowed"].index(row["action"])
                live = log_q[index]
                ratio = torch.exp(live - row["oldLogp"])
                advantage = row["rewardU"]
                width = self.algorithm["clip"]
                surrogate = torch.minimum(ratio * advantage, ratio.clamp(1 - width, 1 + width) * advantage)
                loss = -surrogate / len(rows)
                if not torch.isfinite(loss).item():
                    raise ArithmeticError("Nonfinite clipped PPO loss")
                losses.append(loss.item())
                ratios.append(ratio.item())
                score_errors.append(abs(live.item() - row["oldLogp"]))
                # Activation checkpoint recomputation runs during backward. Keep the same
                # SDPA backend there as in the original complete prefill.
                with self._attention_kernel():
                    loss.backward()
                del log_q, live, ratio, surrogate, loss
            if any(p.grad is None or not p.requires_grad for p in self.parameters):
                raise RuntimeError("Some full-model parameters did not participate in backward")
            named = dict(self.model.named_parameters())
            witness_name = next(name for name in named if name.startswith("model.layers."))
            witness_norm = named[witness_name].grad.detach().norm().item()
            norm = torch.nn.utils.clip_grad_norm_(self.parameters, self.algorithm["gradClip"],
                                                  error_if_nonfinite=True, foreach=False).item()
            if norm <= 0:
                raise ValueError("The supplied real rewards and legal distributions give zero full-model gradient")
            self._dirty = True
            self.optimizer.step()
        finally:
            self.optimizer.zero_grad(set_to_none=True)
        after, new_hashes = _fingerprints(self.model)
        changed = [name for name in hashes if hashes[name] != new_hashes[name]]
        layers = {name.split(".")[2] for name in changed if name.startswith("model.layers.")}
        if not changed or not layers:
            raise ArithmeticError("The optimizer did not change native transformer-layer parameters")
        first = rows[0]
        pi_after, q_after = self._distribution(first["ids"], first["allowed"], False)
        new_pi, new_q = pi_after.exp().tolist(), q_after.exp().tolist()
        witness = {"sampleId": first["sampleId"], "action": first["action"], "allowed": first["allowed"],
                   "inputSha256": first["inputSha256"], "piBefore": first["policy"], "piAfter": new_pi,
                   "qBefore": first["behavior"], "qAfter": new_q,
                   "maxAbsPiChange": max(abs(a-b) for a,b in zip(new_pi, first["policy"])),
                   "maxAbsQChange": max(abs(a-b) for a,b in zip(new_q, first["behavior"]))}
        stats = {"beforeSha256": before, "afterSha256": after, "loss": math.fsum(losses),
                 "actorGradientWitness": {"name": witness_name, "normBeforeClip": witness_norm,
                                          "beforeSha256": hashes[witness_name], "afterSha256": new_hashes[witness_name]},
                 "policyProbabilityWitness": witness,
                 "gradientNormBeforeClip": norm, "ratioMin": min(ratios), "ratioMax": max(ratios),
                 "maxSelectedLogProbabilityDrift": max(score_errors),
                 "parameterCount": sum(p.numel() for p in self.parameters), "parameterTensors": len(hashes),
                 "gradientTensors": len(hashes), "changedTensors": len(changed),
                 "changedTransformerLayers": sorted(int(i) for i in layers),
                 "layerWitness": next(name for name in changed if name.startswith("model.layers."))}
        return stats, new_hashes

    def _accept(self, stats, hashes, cursor):
        self.version += 1
        self.parameter_sha256 = stats["afterSha256"]
        self._tensor_hashes = hashes
        self._cursor = self._committed_cursor = cursor
        self._draw_receipts.clear()
        self._dirty = False
        self.set_mode(self.mode)

    def update(self, samples, journal_path):
        """Commit one online step from actual tool choices and unweighted future interval credits."""
        self._ready()
        if self.mode != "sample":
            raise ValueError("Switch to sample mode before updating with online observations")
        path = Path(journal_path).expanduser().resolve()
        with _journal_lock(path):
            body = dict(self._header(), updates=[])
            if path.exists():
                body, checksum = _load(path)
                if checksum != self._journal_hash or {k: body[k] for k in self._header()} != self._header():
                    raise ValueError("Journal advanced or belongs to another run; restore its committed state first")
            elif self._journal_hash is not None or self.version != BASE:
                raise ValueError("The existing replay chain must be extended at its committed journal")
            if len(body["updates"]) != self.version - BASE or len(body["updates"]) >= self.algorithm["maxUpdates"]:
                raise ValueError("Unexpected journal version or configured bounded replay depth reached")
            rows = self._rows(samples)
            draws = list(self._draw_receipts.values())
            self._validate_rows(rows, draws, self._cursor)
            record = {"fromVersion": self.version, "toVersion": self.version + 1,
                      "startCursor": self._committed_cursor, "endCursor": self._cursor,
                      "draws": draws, "samples": rows}
            body["updates"].append(record)
            if len(canonical(body).encode("utf-8")) + 32 * 1024 > MAX_JOURNAL_BYTES:
                raise ValueError("The actual sample inputs exceed the bounded journal budget")
            stats, new_hashes = self._optimize(rows)
            record["step"] = stats
            self._journal_hash = _save(path, body)
            self._accept(stats, new_hashes, record["endCursor"])
        self._committed_path = path
        return {**stats, "parameterVersion": self.version, "sampleCount": len(rows),
                "newOnlineSamples": len(rows), "newOnlineUpdates": 1, "replayedOptimizerSteps": 0,
                "journalSha256": self._journal_hash}

    def restore(self, journal_path):
        """Reconstruct and verify every committed weight byte from v374, without resampling."""
        self._ready()
        if self.version != BASE or self._cursor or self._journal_hash is not None:
            raise ValueError("Restore requires a freshly loaded v374 actor and an unused sampling cursor")
        path = Path(journal_path).expanduser().resolve(strict=True)
        with _journal_lock(path):
            body, checksum = _load(path)
        if {k: body[k] for k in self._header()} != self._header():
            raise ValueError("Base weights, source, algorithm, seed, or deterministic runtime differ")
        if not 0 < len(body["updates"]) <= self.algorithm["maxUpdates"]:
            raise ValueError("Invalid bounded replay depth")
        count = 0
        try:
            for record in body["updates"]:
                if (record["fromVersion"] != self.version or record["toVersion"] != self.version + 1
                        or record["startCursor"] != self._cursor or record["step"]["beforeSha256"] != self.parameter_sha256):
                    raise ValueError("Broken packet PPO replay chain")
                rows = record["samples"]
                self._validate_rows(rows, record["draws"], record["endCursor"], restore=True)
                stats, new_hashes = self._optimize(rows)
                if stats != record["step"]:
                    raise ValueError(f"Fresh reconstruction differs at v{record['toVersion']}: "
                                     f"expected {record['step']['afterSha256']}, got {stats['afterSha256']}")
                self._accept(stats, new_hashes, record["endCursor"])
                count += len(rows)
        except BaseException:
            self._dirty = True
            raise
        self._journal_hash = checksum
        self._committed_path = path
        return {"parameterVersion": self.version, "parameterSha256": self.parameter_sha256,
                "journalSha256": checksum, "fullFingerprintVerified": True, "replayedSamples": count,
                "nextDrawCursor": self._cursor, "nextDrawUniform": _uniform(self.algorithm["seed"], self._cursor),
                "replayedOptimizerSteps": len(body["updates"]), "newOnlineUpdates": 0, "newOnlineSamples": 0}

    def save_checkpoint(self, path):
        """Save the exact committed replay boundary, including the sampling cursor."""
        self._ready()
        if self._committed_path is None or self._cursor != self._committed_cursor:
            raise ValueError("Save immediately after a committed update or restore")
        dest = Path(path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with _journal_lock(self._committed_path):
            body, checksum = _load(self._committed_path)
        if checksum != self._journal_hash:
            raise ValueError("Committed journal changed before save")
        if dest != self._committed_path:
            with _journal_lock(dest):
                if dest.exists():
                    if _load(dest)[1] != checksum:
                        raise ValueError("A different immutable replay snapshot exists at this path")
                else:
                    _save(dest, body)
        return {"parameterVersion": self.version, "parameterSha256": self.parameter_sha256,
                "journalSha256": checksum, "nextDrawCursor": self._cursor}

    def restore_checkpoint(self, path):
        """The runner supplies the base directory. A registered replay asset selects a later version."""
        source = Path(path).expanduser().resolve(strict=True)
        if source == self._base_directory:
            if self._resume_journal is None:
                return {"parameterVersion": self.version, "parameterSha256": self.parameter_sha256,
                        "replayedOptimizerSteps": 0, "nextDrawCursor": self._cursor}
            source = self._resume_journal
        return self.restore(source)


def build_model(*, checkpoint, tokenizer, device, config):
    if checkpoint is None or tokenizer is None:
        raise ValueError("Packet PPO requires the registered v374 base checkpoint and tokenizer")
    actor = PacketLearner(checkpoint, tokenizer, device)
    replay = config.get("provenance", {}).get("assets", {}).get("replay")
    if replay:
        actor._resume_journal = Path(replay).resolve(strict=True)
    return actor
