# Technical knowledge

The knowledge maintainer publishes reference entries independently of model weights and rule code. A release contains a stable ID, searchable topics, technical content, source paths and applicability. OM supplies the current state and write authority for each work order.

```bash
python run.py knowledge --id K045
python run.py knowledge --topic admission
python run.py knowledge --components config/knowledge-v2.json --topic clock
```

The first command returns the K045 packet-clock entry from edition `1.0.0`. The last returns edition `1.1.0` with a timing note. Both editions are in [knowledge](../knowledge). Query results are JSON arrays. An unknown ID or an unmatched topic returns an empty array. A source/edition mismatch raises an error.

To publish another edition, add a `b03.knowledge.v1` JSON file, keep existing IDs for the same concepts and update the source references. In a component manifest extending [components.json](../config/components.json), set `knowledge.source` relative to the declaring file and set `knowledge.version` to the edition in the JSON. The query reads ordinary local files without starting the packet simulator.

[Read-only lookup](../src/tech_knowledge.py) · [Version switch](../config/knowledge-v2.json)
