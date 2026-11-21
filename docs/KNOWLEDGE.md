# Read-only technical knowledge

The knowledge maintainer publishes a versioned JSON edition. Each entry defines a stable ID, topics, technical content, sources and applicability. The default provider reads edition `1.0.0`. The example provider builds an in-memory index over edition `1.1.0`.

## Query an edition

Run these commands from the repository root. Independent lookup needs Python and the selected knowledge files. It runs without loading a checkpoint or starting the simulator.

```bash
python run.py knowledge --id K044
python run.py knowledge --components config/knowledge-v2.json --topic clock
python run.py knowledge --components config/example-knowledge.json --id K044
```

Each command returns a JSON array. For example, K044 has topic `admission`, references `src/opening.py` and applies at a delivered, writable residual-admission opening. The returned `version` identifies the selected edition. Unknown IDs and unmatched topics return an empty array.

## Replace the provider or the source

Create a manifest extending the shipped component registration. The source path resolves relative to the manifest that declares it.

```json
{
  "extends": "components.json",
  "knowledge": {
    "id": "example.topic-index",
    "version": "1.1.0",
    "source": "../knowledge/technical-v2.json",
    "entrypoint": "example_knowledge:build_knowledge"
  }
}
```

The runner calls `build_knowledge(*, config, library)` with a detached JSON registration and the parsed edition. The returned object implements `lookup(*, identifier=None, topic=None)`. Return a list of matching IDs. Exactly one selector is supplied. An ID matches exactly. A topic matches a case-insensitive substring in any published topic. A trusted local module or installed package can maintain its own index. The runner resolves every returned ID against the pinned source and checks its edition, required fields, unique IDs and matching entries. The provider receives the query and library. OM retains control of write permissions, devices and measurement.

To change the source independently, publish a new `b03.knowledge.v1` file, keep stable IDs for the same concepts and update the source references and version. To pin installed providers or supporting files, register their dependencies using the repository's provenance instructions. [Provider example](../src/example_knowledge.py) · [Edition switch](../config/knowledge-v2.json) · [Provider switch](../config/example-knowledge.json) · [Dependency registration](PROVENANCE.md)

## Inspect an actual RC consultation

The first authorized solver call determines initial admission. In residual mode, RC asks for K044 when OM has delivered a writable pending admission or a new demand revision. The query occurs after tool selection and before the original opening procedure. It records the work order and snapshot, selected action and each triggering demand's revision, observation time and arrival time.

When K044 matches, RC checks the opening result against the original solver proposal. Previously proposed paths stay fixed. Each added service must belong to the authorized missing set. An allocated pending or newly added service must retain its original wire rate. The opening procedure, packet QoS guard and device readback retain their existing roles. The receipt records whether each pending service came from the selected rule, underwent residual search, was added, and reached the final installation request. A run with an edition lacking K044 records an empty match and continues the configured opening procedure.

```bash
python run.py run --policy I --components config/example-knowledge.json --record-component-io --output runs/index-work
python run.py verify runs/index-work --strict-source --components config/example-knowledge.json --simulator "$B03_SIMULATOR" --report runs/index-check.json
python run.py examples runs/index-work --output runs/index-io.json
```

With the engine and data paths from the main README, these commands reproduce the example. `b03_rc_plan.knowledge_io` records the consulted provider and source hash, query, returned entry, technical check and disposition. `examples` exports this actual receipt under `knowledge`. `verify` checks it against the archived edition, OM authority, delivered demand, opening audit and final device plan. Archive-only verification can replay the lookup on a machine without the external provider. The original owner's component and dependency pins support strict source verification. Scenario-derived work-order traces and detailed examples belong with the licensed run assets. [Archive verification](PROVENANCE.md)
