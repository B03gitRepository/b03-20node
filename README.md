# B03 packet network control · 20 nodes

OM reads delivered network and demand reports, opens a work order and grants write authority. RC selects a legal tool and asks the scheduling rule for a joint configuration. The device adapter releases and installs configurations through the packet network. OM signs off using per-hop readback. A fixed rule and a frozen model use this same workflow.

## Install and run

Use Python 3.12 and the pinned **resnet_py 1.1.0** packet engine supplied by the simulator provider. The official topology, service requests and event scripts are distributed separately to protect the data provider’s confidential scenario definitions. Extract the competition package next to this checkout and retain its directory structure. The two network scales can use the same data directory.

```bash
mkdir -p ../b03-data
tar -xf /path/to/competition-data.tar.gz -C ../b03-data
export B03_DATA="$(cd ../b03-data && pwd)"
export B03_SIMULATOR=/path/to/resnet_py
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py run --output runs/default-i
python run.py verify runs/default-i --report runs/default-check.json
```

The expected scenario root is `../b03-data/data/scenarios/sce20261010-conf02/sce01/` with `net-topo.json`, `bg-service.json` and `scripts/`. `--data` can also point directly to that directory or the official archive. The engine can remain read-only. Every run requires a fresh output directory.

The CPU default runs official script **09** with three services, fixed tool **I**, the registered solver and residual initial admission. Use `--services all` for the full scene. `--policy combined` selects a fixed conditional rule. `--protected-services` restricts writes. Run `python run.py run --help` for window, drain and scope parameters.

## Components and owners

| Owner | Interface | Default | Replacement |
|---|---|---|---|
| Model maintainer | `choose(observation)` returns a legal tool and policy version | Frozen v18 with A–C mapping | [Model contract and factory](docs/MODEL.md) |
| Scheduling algorithm maintainer | `enumerate_actions(snapshot, authority, config, history=None)` and `solve_action(action, bound, context=None)` | A–L joint routing tools | [Rule contract and factory](docs/RULES.md) |
| Technical knowledge maintainer | Local read-only query by ID or topic | Technical edition 1.0.0 | [Knowledge schema and switch](docs/KNOWLEDGE.md) |
| Integration maintainer | Component schema, action catalog, packet work-order verifier | `b03.components.v1` / `b03.tools.v1` | [Default component manifest](config/components.json) |

An optional `--components FILE` selects a manifest. The examples extend the default and replace one component. A provider factory may live in this source tree or an installed Python module. Record its release and pin its dependencies. Register new model-visible tool IDs in a new catalog version and update model and rule mappings together.

## Reproduce a replacement

The model path uses a separate local checkpoint. Install the model environment and set the paths below. The example model factory runs the pinned frozen parameters. The example rule implements an alternative allocation order for I.

```bash
python -m pip install -r requirements-model.txt
export B03_CHECKPOINT=/path/to/v18-hf
python run.py run --policy model --device cuda --output runs/default-model
python run.py run --components config/example-model.json --policy model --device cuda --record-component-io --output runs/model-plugin
python run.py verify runs/model-plugin --report runs/model-plugin-check.json
python run.py examples runs/model-plugin --components config/example-model.json --output runs/model-example.json
python run.py run --components config/example-rules.json --policy I --record-component-io --output runs/rule-plugin
python run.py verify runs/rule-plugin --report runs/rule-plugin-check.json
python run.py examples runs/rule-plugin --components config/example-rules.json --output runs/rule-example.json
python run.py knowledge --id K045
python run.py knowledge --components config/knowledge-v2.json --topic clock
```

`examples` exports the actual delivered model observation, chosen tool and policy version, the solver’s authorized snapshot and returned proposal, and a released knowledge entry. Full examples include scenario-derived records and belong with the licensed data. [Measured component examples and regression](docs/COMPONENT_VALIDATION.md) gives the public results and selected fields from these runs.

## Results and code

`CONDITIONS.json` records engine and input digests, the permitted workload and the resolved component identities. `b03.jsonl` contains work orders and readback, and `business-intervals.jsonl` records service loss intervals. `SUMMARY.json` reports U (unmet service-seconds), W (priority-weighted unmet service-seconds), packet counts and timing. The verification report includes actual model calls, selected tools, device activations, verified device queries and measured RC and A2A time.

[OM, RC and authorization](src/packet/packet_roles.py) · [A2A transport](src/packet/packet_a2a.py) · [Device execution](src/packet/packet_device.py) · [Readback](src/packet/packet_device_readback.py) · [Packet and service accounting](src/packet/packet_measure.py) · [Engine and data](docs/ASSETS.md) · [Historical validation](docs/VALIDATION.md)
