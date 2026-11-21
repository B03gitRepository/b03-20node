# Model adapters

`choose(observation)` receives the delivered OM/RC packet observation, schema `b03.packet.rc-choice.v1`. Its `legalActions` are the configured model symbols authorized for this work order. Each `actionCatalog` entry gives the tool, scope, service-set reference, pool, budget and stage. The other tables carry reported topology, measurements, demands and confirmed configurations.

## Adapter interface

Set `model.entrypoint` to `module:factory` in a component manifest. The runner calls the factory once per policy instance.

```python
def build_model(*, checkpoint, tokenizer, device, config):
    ...
```

`config` is the resolved model registration with `id`, adapter `version`, `mode`, `catalogVersion` and ordered `symbols`. If the actor exposes its own symbol mapping, it must match the registration. Return an actor whose `choose` method returns `(tool_id, details)`. A minimal `details` dictionary contains just `policyVersion`, a non-empty string or non-negative integer identifying the actual policy. A frozen policy keeps the same version throughout a run. The native `parameterVersion` is also accepted. When an adapter supplies both keys, they must agree.

The host records the delivered observation, selected tool, adapter ID and selection time. Only report probabilities, hashes or token counts that the model actually computed. `validate_model_choice(choice, policy_identity)` checks a saved `tool_choice` against `CONDITIONS.rcPolicy`.

For an independent model, supply your policy version and registered symbols. The identity kind `frozen-model` selects the native pinned checkpoint, tokenizer and greedy-policy verification. Use it only for an adapter that implements that exact policy.

Each `indexed_table` has `columns`, `defaults` (one per column after the identifier) and `rows`. A row is `[id, mask, *values]`. Start with the defaults. Bit 0 corresponds to the first column after the ID. In ascending bit order, replace each set bit's column with the next value in the row. For example, defaults `[true, 10, 0]` and row `[7, 5, false, 3]` decode to `[7, false, 10, 3]`.

## Frozen reference and work-order examples

Use `--policy model --components config/example-model.json` with the same checkpoint, tokenizer and device as the built-in model. The `example_model:build_model` entrypoint loads the pinned `FrozenActor` and calls its original forward and greedy selection. Both adapters report the weight revision, input token count and hash, legal softmax and behavior probability. Each records its registered `adapterId`.

For an active work-order run recorded with `--record-component-io`, replace `RUN` with the sealed run directory and use the manifest that produced it.

```console
python run.py examples RUN --components config/example-model.json
```

The output's `model.input` and `model.output` are the actual observation and decision, accompanied by the work order and time. The run owner supplies the competition inputs locally.

## Online PPO contract

Sample from the actual behavior distribution over the delivered legal tools. Return the sampled tool's `policyVersion`, full `behaviorProbabilities` and positive selected `behaviorProbability`. Report the legal softmax separately as `legalProbabilities` when available. If supplied, `oldLogProbability` must equal the log of the selected behavior probability.

Implement `save_checkpoint(path)` and `restore_checkpoint(path)` on the actor. When a checkpoint is supplied, the host calls restore after construction and before selecting. The trainer calls save at a consistent update boundary. Restore the parameters, optimizer, version, sampler state and any pending rollout or update data required at that boundary. Validate an online implementation by replaying its next draws and update after a restore. The reference trainer below implements these hooks against real packet work orders.

## CPU dynamic INT8

Install the [model dependencies](../requirements-model.txt) into the packet runtime. Supply the same private checkpoint and tokenizer used by the frozen policy. The x86 or FBGEMM quantized operators must be available in the installed PyTorch build. The example sets four CPU threads.

```sh
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 python run.py run \
  --policy model --components config/int8-model.json --device cpu \
  --checkpoint "$B03_CHECKPOINT" --tokenizer "$B03_TOKENIZER" \
  --data "$B03_DATA" --simulator "$B03_SIMULATOR" --output runs/int8
python run.py verify runs/int8 --strict-source --components config/int8-model.json \
  --checkpoint "$B03_CHECKPOINT" --tokenizer "$B03_TOKENIZER" --simulator "$B03_SIMULATOR"
```

The adapter checks the original weight and tokenizer digests before converting each Linear weight to qint8 in memory. Every forward records the number of converted modules actually called, full-prefill wall time and end-to-end selection wall time. In `CONDITIONS.json`, `rcPolicy.quantization` gives the engine, thread count, conversion time, layer counts and weight element counts. Embeddings, normalization and other floating-point operations retain FP32. A shared embedding/output matrix can appear in both the remaining floating-point embedding and the quantized output projection. Use the Linear element counts to compute conversion coverage. The packet clock charges measured selection time, so long CPU inference can consume the scoring window. The A2A client allows 300 seconds per read by default. Set `B03_A2A_TIMEOUT_S` to a positive finite number of seconds for slower hosts. A transport retry reuses the same message; the RPC receipt records the read timeout, attempt count and any retried error class.

## Complete inputs above the token limit

Inputs within the profile limit use the original chat template and observation. For an over-limit input the adapter groups consecutive default node rows and consecutive IDs within each authorized service set into inclusive ranges. It decodes the result and checks exact canonical JSON equality before tokenizing. The receipt records original and delivered token counts and the message, token and observation hashes. If the reversible representation still exceeds the limit, selection fails with both counts. The work-order trace retains the original delivered observation.

## Online packet training and recovery

The reference training entry point runs in the **100-node profile** with the pinned native v374 checkpoint and its matching tokenizer. Use a CUDA device with BF16 support for the measured training path. The supplied reference trainer uses only official 100-node training scripts `04` and `05`. The work-order verification command also supports archived runs.

Set `B03_BASE` to the private v374 checkpoint directory, `B03_TOKENIZER` to its licensed tokenizer and `B03_DATA` and `B03_SIMULATOR` to the official 100-node inputs and selected packet simulator. Keep the training workspace private. Specify the same original service roster and window for the matched comparisons. This example runs the two small training windows and checks the second checkpoint in a new process.

```sh
: "${B03_TRAIN_SERVICES:?Use the comma-separated service IDs from the authorized training roster}"
for round in 1 2; do
  if [ "$round" = 1 ]; then script=04; else script=05; fi
  python train.py step --workspace runs/ppo --round "$round" --script "$script" \
    --components config/packet-ppo.json --checkpoint "$B03_BASE" --tokenizer "$B03_TOKENIZER" \
    --data "$B03_DATA" --simulator "$B03_SIMULATOR" --device cuda \
    --services "$B03_TRAIN_SERVICES" --start 168 --end 212 --drain 60 || exit 1
done
python train.py restore --workspace runs/ppo --round 2 --checkpoint "$B03_BASE" \
  --tokenizer "$B03_TOKENIZER" --device cuda --report runs/ppo/fresh-restore.json
```

Each round runs the standard A2A/OM/RC packet loop for a fixed-I control and a freshly sampled policy. The factory receives the registered legal symbol list. The reference behavior probability is a 75% policy / 25% uniform mixture on the delivered legal actions. The journal retains the old distribution, selected probability, decision version, full observation and token IDs.

Only samples within the scoring window with at least two legal tools receive credit. The raw return is the negative integral of the number of unmet services from that decision through the scoring end, in service-seconds. The same-window fixed-I return provides the baseline for one clipped PPO step over all trainable parameters. The external evaluation includes W. The optimizer consumes the two unweighted U returns.

Each round has a sealed `sampled` work order, a sealed `fixed-I` control, separate verifier reports and a compact `round-NNN.json` receipt. The receipt's relative paths identify the matching archives. Inspect a sealed work order with `python run.py verify PATH`. For strict source verification, also supply the attempt's `components.json`, simulator, v374 base directory, tokenizer and any registered prior replay snapshot.

`ppo-replay.json` is an atomic working journal. Each `round-NNN-replay.json` is an immutable replay checkpoint with the sample cursor, inputs, outcomes, optimizer settings and parameter hashes. Saving host-side replay checkpoints creates zero additional full model copies. The same base weights, code, numerical runtime and device reconstruct the exact parameters. `restore` checks the full fingerprint and the next sampled action, probabilities and cursor against the live post-update witness. Reissuing a completed round with the same arguments verifies its saved identity. A process that stops just after committing the journal can finish its receipt on retry.

An independently implemented adapter can apply the observation, choice and save/restore contracts above through its own trainer. `src/packet_training.py` exposes `run_episode` to retain a policy instance at construction and the verifier output after the work order. A partner updating its optimizer should publish a new adapter version and use its own replay format or full checkpoint under its data policy.

The reference replay journal holds up to 16 updates and 64 MiB per lineage. Reconstruction uses the pinned base checkpoint. Configure a separate lineage when the training contract or base changes.
