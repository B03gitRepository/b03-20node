# Model group

Start with `python collab/model/check.py` (Python 3.10+, standard library). The complete [input and expected output](case.json) come from three fictitious nodes, two demands and one confirmed route projected through the production observation builder. The [example selector](../../src/standalone_selector.py) uses only delivered data. Test your own module with `python collab/model/check.py --adapter path/to/actor.py`, then set `model.entrypoint` and the ordered `symbols` in a copy of [the manifest](../../config/collab-selector.json). Register external packages and assets as described in [MODEL](../../docs/MODEL.md).

The factory takes keyword arguments `checkpoint, tokenizer, device, config` and returns an actor. On each work order RC calls `choose(observation)`. Return `(legal_tool_id, {"policyVersion": your_version})`. A frozen actor keeps one version. An online sampler records the actual probabilities and supports save/restore as specified in [MODEL](../../docs/MODEL.md). The system solves, applies, reads back and scores the selected tool.

| Observation field | Interpretation |
|---|---|
| `timeNs`, `nodes` | Delivered decision snapshot in simulated nanoseconds. Node indices and observed availability. Entity indices refer to this observation. |
| `links` | Endpoint node indices, observed status and usability, wire bandwidth available to the solver, measured capacity and held wire bandwidth in bits/s. Link delay and jitter in milliseconds, BER and loss as fractions. |
| `services` | Source/destination, type/category, original priority and payload rate in bits/s. Delay/jitter limits in **seconds**, BER/loss limits as fractions. Freshness, activity, revision, diagnosis, writable and protected flags. |
| `configs` | Service index, confirmed path expressed in link indices, reserved payload/wire rates in bits/s, epoch and verification status. |
| `actionCatalog`, `legalActions` | Choose one delivered ID. Its entry defines tool, scope, pool, budget and stage. `serviceSet` indexes `authorizedServiceSets`, each a list of service indices. |
| `budgets` | Path count, path-expansion count, search budget and joint service limit. Use as provided for this work order. |

In each packed table the first value is the entity ID. Start with the column defaults. Bit *i* of the mask replaces the *i*-th default with the next row value, in ascending bit order. See the standalone decoder in [check.py](check.py).

## Sample or evaluate a full episode

Install the standard runner dependencies and set `B03_DATA` using the project README. From the repository root:

```sh
python episode.py sample --script 04 --components config/collab-selector.json --output runs/sample > transitions.json
python episode.py evaluate runs/sample > evaluation.json
```

Both sizes expose the same CLI and Python calls `episode.sample(script, output, ...)` and `episode.evaluate(directory)`. The training script allowlists are 01/02/04/05 for 20 nodes and 04/05 for 100. Use `--services`, `--start`, `--end` and `--drain` to specify a training window. For a development evaluation, use the normal `run.py run` entry point followed by `episode.py evaluate RUN`.

Each training transition carries the delivered observation, action, actual policy version, logged sampling probability when available, end time, and negative unweighted unmet **service-nanoseconds** until the next decision or score-window end. Divide by one billion for seconds. `prefixUnweightedServiceNs` covers the interval before the first included decision. Prefix plus the negated transition rewards reconstructs the whole episode U; simultaneous decisions can have zero-length credit. The separate evaluation returns U/W, official metrics, selections, readbacks and measured time. The reference selector exercises the interface on CPU. Bring your own frozen or online actor and learner to optimize a policy.
