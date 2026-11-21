# Archived component work-order regression

Historical public commit [d13e061](https://github.com/B03gitRepository/b03-20node/commit/d13e0610af11f8acfefa880478a964db52dca485) provides the interfaces and examples measured below. Current opening work orders query K044 when authorized unmet demand is present.

The accepted parent and four component configurations ran on the same pinned simulator, official input bytes, write scope, seed and measured window. Each row is an actual work-order run. The model rows used frozen parameters.

| Configuration | Active RC choices | U (s) | W (weighted s) | Device activations | Verified device queries | RC selection / solver / charged total (s) |
|---|---|---:|---:|---:|---:|---:|
| Parent · fixed I | I × 2 | 15.328 | 78.739 | 5 | 104 | 0.0000 / 0.0253 / 0.0253 |
| Default · fixed I | I × 2 | 15.328 | 78.739 | 5 | 104 | 0.0000 / 0.0312 / 0.0312 |
| Replacement rule · fixed I | I × 1 | 4.156 | 23.284 | 3 | 78 | 0.0000 / 0.0244 / 0.0401 |
| Parent · frozen model | C × 2 | 17.000 | 88.123 | 5 | 104 | 0.5983 / 0.0204 / 0.6187 |
| Default · frozen model | C × 2 | 17.000 | 88.123 | 5 | 104 | 0.5915 / 0.0199 / 0.6114 |
| Replacement model adapter | C × 2 | 17.000 | 88.123 | 5 | 104 | 0.6154 / 0.0196 / 0.6350 |

The original and default routes had identical initial proposals and identical lists of activated paths, rates and epochs. The two frozen adapters used the same weight digests and selected the same first action from the same input and token probabilities. U and W include the packet clock. Each row records its own measured decision time. Required external component checks are charged before dispatch. Trace export time is recorded separately.

## Measured interface examples

These fields come from the replacement runs above. The README commands reproduce the complete local input and output for the partners holding the original scene.

| Component | Actual input (selected fields) | Actual output (selected fields) |
|---|---|---|
| Model | Schema `b03.packet.rc-choice.v1`, legal tools `A B C` | Tool `C`, policy version `18` |
| Rule | Tool `I`, budget `medium`, a snapshot and its work-order authority | 3 service entries, solver audit `residual-first bounded pair search` |
| Knowledge | ID `K045`, edition `1.0.0` | Packet clock and work-order readback, 3 source references and an applicability field |

The query for K045 in edition 1.1.0 adds the separation of RC compute time, network transit, the score boundary and the drain ledger. It runs without loading model weights.

## Reproduce

Follow [the setup and six configurations](../README.md). For an independent source-only contract check, run `python -B -m unittest discover -s tests`. Preserve the per-run `CONDITIONS.json`, `RUN_FILES.json` and verifier report to identify the exact input and implementation.
