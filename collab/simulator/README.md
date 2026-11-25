# Simulator team

The default backend is the pinned packet engine. A v2 extension receives four detached data records. Replacing the extension takes one manifest change. The host uses the same OM/RC, packet clock, command path, device readback and business meter for both implementations.

## Try it

```sh
python collab/simulator/check.py
python collab/simulator/check.py --provider my_detector.py --factory build --fixture my-case.json
```

The self-test needs Python 3.10+ and its standard library. [case.json](case.json) contains a fictional link, a delayed probe, a device query and one measured interval. [The example provider](../../src/example_public_simulator.py) implements a device-local probe triggered by a link change.

## Implement the four callbacks

`build(settings)` returns an object with `id`, `version`, `contractVersion = "b03.packet-simulator.v2"` matching its registration. All callback times are integer simulation nanoseconds. Field names and complete example records are in the fixture.

| Callback | Input | Output and effect |
| --- | --- | --- |
| `on_event(record)` | After one physical batch, its sequence, time, change types, changed link endpoints, eligible `probeNodes` and remaining window. | Return a list of `{"op":"probe","nodeId":...,"delayNs":...}`, at most once per eligible node. The bridge schedules physical sampling after the positive delay, before the window closes. An online node sends its regular probe packets through actual queues. |
| `on_observation(record)` | A probe or service report arrived at the controller. The record includes its original sample time, arrival time, packet ID, acceptance, reason and delivered message. | Return `None`. The native delivered cache determines what OM/RC can read. |
| `on_device(record)` | Separate records for node execution and the acknowledgment reaching the controller. They include command/packet ID, status, version and execution clock. A query reply includes its node-local sample in `readback`. | Return `None`. Work-order instructions use the existing device command channel. |
| `on_meter(interval)` | One completed half-open business interval with active/unmet counts and U/W increments in business-nanoseconds. | Return `None`. The evaluator derives these values from native device paths and original requirements. |

Callback inputs are copies. The simulator extension is trusted Python; its data callbacks are kept separate from the roles. `changes` reports the current physical batch inside the backend. Only a later delivered packet reaches the controller. Evaluator intervals are confined to the simulator/evaluator side.

## Run on the shared conditions

Use the repository's configured Python environment and set `B03_DATA` to the licensed dataset. The example manifest selects the fresh provider for each run.

```sh
python run.py run --components config/collab-simulator.json --output runs/probe-demo
python run.py verify runs/probe-demo --strict-source --components config/collab-simulator.json
```

Set `simulator.entrypoint` to `your_module:build` and replace the ID, version and settings in the [manifest](../../config/collab-simulator.json). External packages declare their wheel or local source in [provider provenance](../../docs/PROVENANCE.md). The run summary records callback counts and the number of requested probes. Verification compares those counts to sealed native events, controller deliveries, executions and business intervals, and matches requested probes to changed endpoints and completed clock callbacks. The [backend notes](../../docs/SIMULATOR.md) cover engine compatibility.
