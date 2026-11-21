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

## Frozen reference and actual examples

Use `--policy model --components config/example-model.json` with the same checkpoint, tokenizer and device as the built-in model. The `example_model:build_model` entrypoint loads the pinned `FrozenActor` and calls its original forward and greedy selection. Both adapters report the weight revision, input token count and hash, legal softmax and behavior probability. Each records its registered `adapterId`.

For an active work-order run recorded with `--record-component-io`, replace `RUN` with the sealed run directory and use the manifest that produced it.

```console
python run.py examples RUN --components config/example-model.json
```

The output's `model.input` and `model.output` are the actual observation and decision, accompanied by the work order and time. The run owner supplies the competition inputs locally.

## Online PPO contract

Sample from the actual behavior distribution over the delivered legal tools. Return the sampled tool's `policyVersion`, full `behaviorProbabilities` and positive selected `behaviorProbability`. Report the legal softmax separately as `legalProbabilities` when available. If supplied, `oldLogProbability` must equal the log of the selected behavior probability.

Implement `save_checkpoint(path)` and `restore_checkpoint(path)` on the actor. When a checkpoint is supplied, the host calls restore after construction and before selecting. The trainer calls save at a consistent update boundary. Restore the parameters, optimizer, version, sampler state and any pending rollout or update data required at that boundary. Validate an online implementation by replaying its next draws and update after a restore. The reference adapter exercises frozen inference.
