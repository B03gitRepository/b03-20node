# Replace the rule solver

For the v2 contract, runnable example and standalone test, see the [rules group kit](../collab/rules/README.md). The original plugin registration described below remains available.

A rule component receives one delivered OM/RC work order and proposes assignments. The packet controller checks the proposal, issues device commands and verifies their acknowledgements. The default manifest uses the shipped strong tools. The example switches the rule component while retaining the same tool catalog and fixed I selection.

With the Python environment, simulator and official input from this repository's README, run the following commands from the repository root. Choose a new output directory for each run.

```bash
python run.py run --policy I --components config/example-rules.json --record-component-io --output runs/residual-example
python run.py verify runs/residual-example
python run.py examples runs/residual-example --components config/example-rules.json --output residual-example.json
```

The example in `src/example_rules.py` changes I's bounded pair search. At each original priority it first considers demands with fewer feasible routes. It orders candidate paths by their projected link utilization as earlier demands take capacity. It then uses I's original pair and single-service neighborhoods, path limits and evaluation ceiling. The other symbols use their original solvers. A source-only regression gives one demand two feasible parallel routes. Original I selects the faster link and the example selects the link with more available capacity.

## Provider interface

Set `rules.id`, `rules.version` and `rules.entrypoint` in a manifest extending `config/components.json`. The example uses `example_rules:build_rules`. The runner imports a trusted local Python module and calls `build_rules(*, config)` once per chooser. The factory receives the resolved rule descriptor, including any `options`. Its object supplies two methods.

| Method | Input and return |
| --- | --- |
| `enumerate_actions(snapshot, authority, config, history=None)` | Return a list of available structured actions. Each has a registered ID, method, scope with service IDs, candidate pool, budget and stage. The controller verifies those fields against its own catalog for the current reports and permissions. |
| `solve_action(action, bound, context=None)` | Return `(target, audit)`. Provide an entry for every service in `bound['snapshot']['requests']`. Each value is `None` or a path with `nodes`, `linkIds` and `allocatedBps`. Finish with `rule_component.bind_proposal(action, bound, target, audit)` to bind the return to this work order, its snapshot, authority, budget configuration, selected action and target. |

`bound` carries `identity`, `snapshot`, `authority` and `config`. The snapshot carries observed nodes and links, requests, current assignments and observation time. Its allocation units are wire bits per second. Each request also carries its original payload rate. Return the full requested wire rate and meet delay, jitter, BER and packet-loss limits. Available capacity already accounts for retained reservations and control headroom. Keep healthy and protected assignments fixed. Only the selected tool's writable services can change. The owner of a rule can offer a subset of the catalog. Fixed and combined policies still require their chosen symbols for active work.

The controller passes detached inputs and retains a private solver context for the lifetime of the chooser. Catalog enumeration receives a copy of that context. A solver can keep a route cache in its context across work orders. The controller independently runs the original proposal validator against its current snapshot and checks the complete roster, write scope, path contract, change budget and declared search counts. Opening admission and the native packet QoS check still run before device commands.

The published `b03.tools.v1` catalog defines A–L. To add an alias for an already authorized native template, register the new symbol in `catalog.symbols`, set a new catalog version and declare the mapping in `rules.options.toolAliases`, for example `{"M":"I"}`. Set the model and rule catalog versions to the same new version. Include the symbol in the model mapping if the model should select it. The alias retains its template's authorized method, scope, pool and budget.

## Register an independent pure rule

The M example runs a deterministic route search written by the rule provider. Its priority queue explores currently usable links in decreasing order of residual bottleneck capacity, with delay and link sequence as tie breakers. The provider deducts each accepted reservation before considering the next authorized demand. It checks the original bandwidth, delay, jitter, BER, loss and hop limits. Controller-side validation, packet installation and per-hop device readback use the same interfaces as the default solver. [Solver source](../src/branch_rules.py) · [Registration](../config/branch-rules.json)

```bash
python run.py run --components config/branch-rules.json --policy M --record-component-io --output runs/branch-m
python run.py verify runs/branch-m --strict-source --components config/branch-rules.json --simulator "$B03_SIMULATOR" --report runs/branch-check.json
python run.py examples runs/branch-m --output runs/branch-io.json
```

The component owner implements `build_rules(*, config)` and the two provider methods described above. The integration maintainer registers the extra symbol in a new catalog, gives the model and rule descriptors the matching catalog version, and leaves existing model symbols in their original order. The example adds the following declaration to `rules.options`.

```json
{
  "customTools": {
    "M": {
      "template": "L",
      "tool": "widest_residual",
      "pool": "delivered_neighbors",
      "budget": "medium"
    }
  }
}
```

The controller derives M's current writable service IDs from template L and OM's delivered authority. The registration fixes the extra method, pool and the template's named search budget. The provider can offer M only when the controller authorizes L on this snapshot. In a custom-tool configuration, the controller retains and solves its registered native A–L actions using the original implementations. The example model mappings keep their previously trained symbols. Fixed `--policy M` exercises the new provider directly.

M returns the full service roster with a bound audit. Every active service in its scope has integer `pathSearch.expanded` and `pathSearch.selectedCandidates` counters, including zero for an empty result. Each row uses its service ID as the map key. The example caps expansions at 8,000 per service and commits at most one selected path per service. A provider receives detached inputs and a private context copy. Its context becomes available to the next work order after a successful proposal and final packet checks. The controller validates reported counts, enforces the scope, original wire rates and physical constraints on the actual target, and measures call time.

To replay M from a recorded export, use the same installed rule dependencies and run this code in the repository root.

```python
import json
import sys
from pathlib import Path

sys.path[:0] = ["src", "src/rules", "src/rules/vendor/src"]
from component_config import load_components
from rule_component import CheckedRules

sample = json.loads(Path("runs/branch-io.json").read_bytes())["rules"]
components = load_components("config/branch-rules.json").activate("M")
provider = components.factory("rules")(config=components.rules)
rules = CheckedRules(provider, components.rules, components.symbols, sample["context"])
catalog = rules.catalog(sample["bound"], {})
assert catalog == sample["catalog"]
target, audit, checked = rules.solve(sample["action"], [a["id"] for a in catalog], sample["bound"], {})
assert checked["valid"] and {"target": target, "audit": audit} == sample["returned"]
```

Use `config/branch-rules-knowledge.json` to select both this rule and the indexed knowledge edition. The independent switch examples above use `config/branch-rules.json` for M and `config/example-knowledge.json` for the knowledge provider with the original fixed I. [Knowledge lookup](KNOWLEDGE.md)

## Expiry and readmission

OM opens a release-only work order when a delivered demand report shows that a writable service has stopped and current device evidence confirms its old epoch. An unverified configuration first goes through a read-only refresh. Healthy services, protected services and services outside the whitelist keep their assignments. When expiry coincides with active work, `lifecycle_release.release_ids` names the old configurations to retire and `deferred_active_ids` names the active writes to reconsider.

RC builds this release directly. The receipt has `tool_choice.method="lifecycle"`, `selected=null`, `modelCalled=false` and `real_solver_calls=0`. The measured `lifecycle_wall_ns` is charged to `decision_wall_ns`. The device loop rechecks the delivered revision before deactivation, observes the old-rule retention interval, sends cleanup to all path nodes and independently queries each node. The old wire reservation stays charged until the final matching query arrives. OM then checks the receipts. The next work order links to that sign-off through `reassessment_after_release`, retains its delivered observation snapshot even for an idle decision, and uses the configured policy to reconsider current demand.

## Search and change budgets

In `b03.tools.v1`, I uses `pair_local`, the `low_overlap` candidate pool and the `medium` budget from `src/rules/strong_tools.py`. The example keeps the following limits.

| Operation | Limit |
| --- | ---: |
| Path expansions per service | 8,000 |
| Paths requested from the generator per service | 32 |
| Candidate paths kept per service | 8 |
| Services in one local move | 2 |
| Trial assignments across all local passes | 4,000 |
| Local passes | 3 |

A valid current path can also enter the pool before the eight-path cutoff. `config.max_path_hops` limits each new path when set. `config.t096_max_changed_services` limits the total number of changed assignments, including releases. The default is the number of executable services. The provider reports `localEvaluations`, `passes`, `maxChangedServices` and per-service `pathSearch`. The controller checks the change count against the actual target and checks supplied search counters against the selected budget. Each trusted Python provider implements its own search limits.

## Work-order evidence and timing

`--record-component-io` stores the actual action, catalog, bound input, pre-call context and raw returned target/audit under `b03_rc_plan.component_io`. The raw return precedes opening admission and the packet QoS check. The same record's solver audit and device plan show any subsequent adjustment. The `examples` command extracts a recorded work order from a verified run.

For an external solver, the device dispatch clock charges the complete RC operation from work-order binding through final plan assembly, excluding optional sample-copy time. `decision_wall_ns` equals `tool_choice.selectionWallNs + solver_wall_ns + component_timing.chargedComponentWallNs`. `solver_wall_ns` includes the solver call and opening admission. The charged component remainder includes setup, catalog enumeration and validation, input isolation, independent proposal validation and final plan checks. `component_timing` also reports `setupWallNs`, `catalogHookWallNs`, `catalogGuardWallNs`, `solverHookWallNs`, `proposalGuardWallNs` and `postProposalWallNs`. These breakdowns overlap the charged totals. `sampleCopyWallNs` records optional capture work outside the dispatch clock. Transport and logging happen after the proposal returns.

For the built-in solver, `decision_wall_ns` retains the release definition `tool_choice.selectionWallNs + solver_wall_ns`. For an external solver, `setupWallNs` includes binding the work order and initializing the provider on its first call. `catalogGuardWallNs` and `proposalGuardWallNs` include the required controller-side preparation and validation around each hook. `postProposalWallNs` covers packet QoS and the device plan, wire ledger and snapshot digest. The individual hook and guard values describe parts of the operation and should not be added a second time to the charged total.

## Replay the exported I proposal

After running `verify` and `examples` above, run this Python code from the repository root in the same environment. It verifies the recorded component identity and tool, then compares the whole target and audit from the deterministic example I with the archived return.

```python
import json
from pathlib import Path
import sys

sys.path[:0] = ["src", "src/rules", "src/rules/vendor/src"]
from component_config import load_components
from strong_tools import validate_proposal

sample = json.loads(Path("residual-example.json").read_bytes())["rules"]
conditions = json.loads(Path("runs/residual-example/CONDITIONS.json").read_bytes())
components = load_components("config/example-rules.json")
assert components.identity == conditions["components"]
assert sample["component"] == {k: components.rules[k] for k in sample["component"]}
action, bound = sample["action"], sample["bound"]
assert action["id"] == "I" and action in sample["catalog"]
rules = components.factory("rules")(config=components.rules)
target, audit = rules.solve_action(action, bound, context=sample["context"])
assert {"target": target, "audit": audit} == sample["returned"]
assert validate_proposal(bound["snapshot"], bound["config"], bound["authority"], target)["valid"]
print(sample["work"], audit["targetHash"])
```

Other tools can report measured CPU or wall time in their audit. Compare the returned target, binding and deterministic search fields for those tools. Use `verify` on the original run to check the device acknowledgements and packet accounting.

Run the rule tests with `python -B -m unittest discover -s tests -p test_rule_component.py`. Run the complete repository suite with `python -B -m unittest discover -s tests -v`.
