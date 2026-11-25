# Packet simulator adapter

The default work order uses the published `b03.resnet` adapter and the pinned `resnet-simulation` 1.1.0 packet engine. The engine applies official topology, demand and event inputs on a SimPy nanosecond clock. OM and RC exchange detached observations through A2A. Commands, acknowledgments and per-node configuration queries travel over the simulated packet network. The host meter evaluates installed paths, QoS and actual demand independently.

## Public extension

For a new simulator extension, start with the [four public data ports, standalone example and test](../collab/simulator/README.md). The work-order host uses delivered snapshots and a single command transmitter. The backend bridge owns the native control-loop attachment, node-local readback sampling, physical clock hooks and evaluator snapshot. The packet engine continues to supply its native queue, transport and device state machine.

## Backend implementation boundary

The native bridge is the single place that attaches the simulator's control loop and node executors, reads installed forwarding state, and maps physical batches into public events. Changes to a callback provider use only the four inputs above. When changing the native backend, preserve these host-facing operations:

| Operation | Used by the host |
| --- | --- |
| Clock and delivery | `advance(until_ns)` processes native events in order, including same-time priorities. `start_ns`, `end_ns`, `stop_ns` and `now` are integer nanoseconds. `summary()` returns packet, device and conservation counters at the scoring and drain boundaries. |
| Delivered evidence | `connect_work_order(roles, emit, ...)` binds one work loop to read-only link/demand snapshots at a given clock, the delivered demand specification, controller log and the sole command sender. OM/RC receive the existing detached role inputs. |
| Device | The native command path serializes, transmits, executes and acknowledges each instruction. The bridge attaches one local reader per node; a duplicate query replays its first sample and packet identity. |
| Measurement | `start_meter(directory, solver)` schedules scoring snapshots after same-time native changes. `meter_snapshot()` supplies physical nodes, links, active original demands and end-to-end installed assignments only to the evaluator; `payload_wire_bps(rate)` accounts wire cost. `finish_meter()` requires the inclusive drain advance to have returned. It closes the action port, replays terminal device and meter records, and produces final U/W, denominators and connectivity. `close_meter()` and `close()` release resources. |

## Native v1 compatibility

Set `B03_DATA` to the official scenario folder, extracted dataset or archive. The default engine is `simulator/resnet_py/` in this checkout. Use `--simulator` or `B03_SIMULATOR` to select an external release (CLI first). Use a fresh output directory for each run.

```sh
python run.py run --output runs/periodic
python run.py run --components config/example-simulator.json --output runs/local-probes
python run.py verify runs/local-probes
python run.py verify runs/local-probes --strict-source \
  --components config/example-simulator.json
```

The example implements device-local link-change detection. Each endpoint of a link whose parameters or administrative status changed waits the configured detection interval, samples its own ports and sends the usual native probe packets. The default interval in the example is 200,000,000 ns. An offline node cannot send. Network queues, loss, both endpoint reports, observation freshness and device readback remain part of the work order. The baseline uses its original periodic probes. Compare the saved event and both received observations in `SUMMARY.json` and `controller.jsonl`. Earlier detection can be measured separately from U/W, packet cost and configuration completion.

## Provider interface

A simulator component specifies `id`, `version`, `contractVersion`, `entrypoint` and an optional JSON object named `settings`. The accepted contract is `b03.packet-simulator.v1`. The entry point is an importable Python factory.

```json
{
  "extends": "components.json",
  "simulator": {
    "id": "example.local-port-probes",
    "version": "1.0.0",
    "contractVersion": "b03.packet-simulator.v1",
    "entrypoint": "example_simulator:build",
    "settings": {"detectionDelayNs": 200000000}
  }
}
```

`build(settings)` receives a detached object. It returns a subclass of the selected engine's `resnet.simulation.simulation.Simulation`. The class declares `adapter_id`, `adapter_version` and `contract_version`. These values must match the manifest. The constructor accepts the native scenario, simulation configuration, run directory and `decision_policy` keyword. The runtime uses the native SimPy clock, packet links and queues, controller, local node executors, command/query interfaces, state, counters, `advance`, `summary`, `log` and `close`. A compatible provider must preserve these interfaces and their delivery semantics. The host composes its own measurement hooks with the selected class and attaches the existing device loop. [Working provider](../src/example_simulator.py) · [Adapter and identity checks](../src/simulator_component.py)

Physical event inputs and true link or device state belong to the simulator and the evaluator. Device probes sense local state. The controller dates observations when the corresponding packets arrive. OM and RC receive copies of the configured scopes, delivered reports and query replies. A provider runs as trusted Python code in the same process as the engine. The role interface does not grant access to the simulator, future events, coordinates or U/W scores.

## Use a compatible engine release

Place the separately supplied source release outside the checkout. The host imports `resnet` from that release's `src` directory. For a different release, register a complete engine lock as the simulator's `provenance.assets.engineLock`. The path is relative to the manifest that declares it. Other provider packages, helpers and assets follow the same provenance rules as the model and rules components. [Provider and dependency records](PROVENANCE.md)

```json
{
  "extends": "../config/example-simulator.json",
  "simulator": {"provenance": {"assets": {"engineLock": "partner.lock.json"}}}
}
```

The lock contains `distribution`, `version`, `files` and `runtimeSha256`. The file map uses release-relative POSIX paths and lowercase SHA-256 digests. Include every importable file under `src`, the native `configs/common.json`, the package's `pyproject.toml` and any additional input or implementation files read by the replacement engine. Its project name and version must agree with the lock. Compute the runtime digest from the sorted file map using the following expression.

```python
hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
```

Run with both the partner manifest and the actual engine path. Supply the official inputs explicitly through `--data` or `B03_DATA`. The simulator checks the selected runtime before and after execution. The run seals the exact lock bytes, adapter identity and runtime fingerprint along with its original inputs and packet receipts. Archive verification rechecks those records without importing the provider. Strict verification also compares the installed engine files and the source, assets and dependencies registered by the selected provider.

Official scripts with `node_move` use `endpoint_at_event` when supplied through `--data`. At the event timestamp the native batch places the node at the specified destination coordinates. The official input provides no movement interval. Link changes are driven by the supplied link events and the engine's native device state. A coordinate in the evaluator's batch trace does not constitute a delivered network observation.

## Defer an unreachable cleanup

The default device loop waits for an unresolved cleanup. To let unrelated work proceed during a long outage, enable this work-order option when starting the run.

```sh
python run.py run --mode workorder --defer-unreachable-cleanup --output runs/deferred-cleanup
python run.py verify runs/deferred-cleanup
```

The controller may defer a terminal work order that releases exactly one service, has received the source-stop acknowledgment, retains the whole previous epoch and has an expired cleanup write followed by an unanswered reconciliation query. The native transaction deadline must have elapsed. The controller protects that service, all links carrying its old wire reservation and the unreachable device. Another work order may use disjoint links and another target device. A healthy router shared between those paths applies writes serially with its current device version.

The controller can resume the old work order after it receives a new, fresh native probe from the unreachable device. It first sends a new query for the old service and epoch. The returned state determines whether another cleanup command is required. The controller returns the held capacity only after complete per-node readback and records the OM result. The terminal summary lists unresolved epochs under `b03LoopStatus.deferred_cleanups`. The verifier checks the retained ledger, the recovery probe and the order of subsequent device operations.
