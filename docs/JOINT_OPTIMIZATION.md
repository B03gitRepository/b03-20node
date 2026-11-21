# Joint optimization guide

Both network sizes use the same work-order and component contracts. Select the repository whose profile matches your official input. Start with the default work order in the [README](../README.md). Keep its verification report as your comparison.

| Team | Owns | First integration example | Evidence to return |
|---|---|---|---|
| Model | Tool selection, observation encoding, numerical precision, sampling and checkpoint recovery | [Model interface](MODEL.md), `config/int8-model.json` | Legal choices and policy version. For training, include the probability actually sampled, two parameter updates and a fresh-process restore. |
| Simulation | Packet queues, report transport, device execution and compatible simulator releases | [Simulation interface](SIMULATOR.md), `config/example-simulator.json` | Engine identity, matched report times, command IDs and per-hop readbacks. Separate network waiting from solver time. |
| Knowledge and scheduling rules | Versioned technical entries and read-only retrieval, candidate paths and joint configuration algorithms | [Knowledge](KNOWLEDGE.md), [rules](RULES.md), `config/example-knowledge.json`, `config/branch-rules.json` | Entry IDs, sources and applicable conditions. For a rule, supply the authorized scope, proposal, audit and measured result. |
| Systems integration | OM/RC and A2A, authority, action catalog, device writer, scoring, regression and release | `config/components.json`, `run.py verify` | Same-input comparisons, archived receipts, version mapping and a tested release commit. |

## Submit one change

1. Branch from the published commit and note the repository scale. Record the baseline commit, scenario fingerprint, seed, window, drain interval, allowed services, component versions and model checkpoint identity where applicable.
2. Add a configuration extending `config/components.json`. Change one component at a time. Version its implementation and keep portable assets next to the manifest or in an installable package. The [provenance format](PROVENANCE.md) supports external distributions and versioned assets.
3. Run the baseline and the candidate against your licensed data and read-only simulator. Reuse the same CPU settings and model warm-up convention. Use a fresh output directory each time.
4. On the candidate, run archive verification, strict verification with its installed dependencies, and example export. Include the exact replay command and a short aggregate table in your pull request. Keep raw scene-derived traces, datasets and weights in the authorized shared workspace.
5. The integration team checks both network sizes, records which component versions passed, and promotes the tested Git commit. New model-visible tool meanings or observation encodings get an explicit version and a held-out comparison with the original policy.

```bash
python -B -m unittest discover -s tests
python run.py run --record-component-io --output runs/reference
python run.py run --components config/example-simulator.json --record-component-io --output runs/simulator
python run.py run --policy M --components config/branch-rules.json --record-component-io --output runs/pure-rule
python run.py run --components config/example-knowledge.json --record-component-io --output runs/knowledge
python run.py knowledge --components config/example-knowledge.json --id K044
python run.py verify runs/simulator --report runs/simulator-check.json
python run.py verify runs/simulator --components config/example-simulator.json --strict-source --simulator "$B03_SIMULATOR" --report runs/simulator-strict.json
python run.py examples runs/simulator --output runs/simulator-examples.json
```

For the model example, install the model dependencies and set the checkpoint and tokenizer paths described in [MODEL.md](MODEL.md). The fixed and model policies both pass through OM/RC, A2A, the same writer and per-hop readback. The fixed path chooses its configured tool without a model call.

## Compare results

`SUMMARY.json → competitionMetrics` uses the simulation clock in nanoseconds. Each distribution gives its sample count, mean, nearest-rank P95 and maximum. Empty samples have null durations. The primary summaries cover the scoring window. `includingDrain` extends measurement through device cleanup. Inspect individual records when plans were rejected, unfinished or had no device change.

| Metric | Published definition |
|---|---|
| State perception | Official in-window event to arrival of fresh matching state reports. A link change requires matching reports at both endpoints. A node outage without a matching delivered report remains in the event denominator. Position-only movement has no coordinate report in the current contract. |
| Strategy generation | Latest fresh report in the bound OM snapshot to the computed plan's ready time. Per-work-order `generationComputeNs` isolates measured RC computation. Missing fresh evidence is reported separately. |
| Strategy dispatch | First configuration write sent to last distinct configuration write delivered, for a signed work order. Retried commands share one ID. First issue, last observed device effect and final OM sign-off each have their own timestamp. |
| Connectivity | Time-weighted pair connectivity among online nodes. Active service endpoint reachability has a separate denominator. |
| Service guarantee | Requested service-seconds, unserved service-seconds U and served fraction. W is priority-weighted unserved service-seconds for evaluation. Data delivery and drops remain in the packet counters. |

Demand bandwidth, QoS, priority, permissions and clocks come from the recorded work order. Technical knowledge is advisory. Resources remain reserved until the corresponding device release is confirmed. The model sees the arrived decision evidence and the legal tool catalog. Offline scoring can use simulator state after the episode.

[中文协作交接](COLLABORATION_zh.md)

Scored denominators accompany each time distribution. A fresh arrival timestamp is recorded for every plan, including holds without a configuration change. Coverage separately counts plans without fresh evidence, plans ready after the window, rejected-proposal fallback holds and verified configurations. Reachability, requested service-seconds and the service ratio are checked against the interval ledger.
