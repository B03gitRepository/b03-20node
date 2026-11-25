# Run evidence and source verification

Current work orders declare `CONDITIONS.receiptContract = b03.packet.workorder.v2` and seal a `b03.packet.run-files.v2` member index. Archive verification reads recorded contracts and component identities. For a strict replay of an earlier release, use that original checkout, engine and active assets with `run.py verify RUN --strict-source`.


`python run.py verify RUN` rechecks the saved work order on the packet clock. It checks the archived file digests, the official scenario and simulator release identity, service permissions, QoS, path activation, per-hop device readback, packet conservation and the U/W integrals. It can run from a later checkout for the same official scenario profile. The model adapter, weight directory and solver wheel can all be absent. Keep the original `RUN_FILES.json` digest with the release record if you need an independent reference for the archive.

## Inspect a saved run

```console
python run.py verify RUN --report audit.json
python run.py examples RUN --output samples.json
```

The report identifies the verification mode and the SHA-256 of `RUN_FILES.json`. The example exporter reads a recorded RC choice and solver proposal from `b03.jsonl`. Use `--record-component-io` when creating a run if you need the complete solver input and output. New runs include the selected technical knowledge edition in `CONDITIONS.provenance.knowledge`. Its original UTF-8 bytes and digest identify the edition used for the accompanying K045 query.

A run from `components-v1.0.0` already records the provider entry-module hash, full checkout inventory and selected knowledge digest. The current verifier can audit the archived business outcome after an upgrade or removal of that plugin. The exporter uses a local knowledge file only when its digest matches the archived edition. If that copy is unavailable, the result marks the knowledge sample `legacy-source-unavailable` and retains the recorded work-order samples. Runs predating component registration identify their knowledge sample as `knowledge-unrecorded`.

## Compare the original implementation and environment

Restore the recorded Git release, Python environment and any external assets from their original storage. Use the manifest that selected the provider.

```console
python run.py verify RUN --strict-source --components /path/to/partner.json --simulator /path/to/resnet_py
python run.py verify MODEL_RUN --strict-source --components /path/to/model.json --checkpoint /private/weights --tokenizer /private/tokenizer
```

Strict comparison checks the checkout’s Python sources, including helpers available to partner implementations, resolved component settings, Python version and platform, installed distributions, and active model assets. With `--simulator` or `B03_SIMULATOR`, the verifier rereads each pinned file from the local packet engine and reports `simulatorCheck` as `installed-files`. Without an engine path it checks the archived release against the public lock and reports `release-lock-only`. A source export records file digests and can be located through the release index. Each discrepancy identifies the component, distribution, file or asset. Fixed-tool work orders do not require model weights. Edits to an unselected knowledge edition or an unused manifest in the same checkout leave the effective comparison intact. The report lists the recorded and current Git commits when available. Equivalent source content may pass after a merge or a commit that changes an unused manifest.

The built-in Qwen actor continues to check every configured tokenizer file and every registered weight against `config/model.json`. The 20-node release uses the registered safetensor shards and index. The 100-node release uses the registered native checkpoint and metadata pair. A wrapper reporting this native actor identity follows the same checks. An independent model can accept a checkpoint file, a directory of weight shards and an optional separate tokenizer. The host hashes their actual bytes when supplied on an active model run. Use a stable checkpoint taken before the run for online adapters.

For a v1 run, the original tag and command remain available.

```console
git worktree add ../b03-original components-v1.0.0
cd ../b03-original
python run.py verify /path/to/original-run
```

To rerun the simulation, use that tag's documented `run` command with the original scenario, simulator, manifest, weights, service scope and clock window. A legacy receipt's entry-module digest cannot establish the contents of an unrecorded sibling package or dependency. Retain the original environment or wheel with that experiment.

## Register external sources and assets

Install a partner wheel in the Python environment running B03. Its import name and distribution name can differ. B03 locates the distribution that owns the actual entry-module file. An explicit `distribution` resolves ambiguous package names. The same provenance fields are accepted in the `model` and `rules` registrations.

```json
{
  "extends": "components.json",
  "model": {
    "id": "partner.policy",
    "version": "2.1.0",
    "entrypoint": "partner_policy.adapter:build",
    "provenance": {
      "distribution": "partner-policy",
      "distributions": ["partner-kernels[fast]==1.4.0"],
      "assets": {"calibration": "../private/calibration.bin"}
    }
  }
}
```

Place this manifest beside the base `components.json`, or adjust `extends` to the location of the published base manifest. Each asset path resolves against the manifest that declares it, including inherited registrations. The logical asset name and content digest travel in the receipt. Register out-of-package configuration, lookup tables and model files opened by the implementation. For dynamically selected Python dependencies, list their distribution names or PEP 508 requirements in `distributions`.

The collector hashes the actual installed files in the selected distribution, including helper modules, compiled extensions, package data and metadata. It follows active `Requires-Dist` declarations transitively, applying Python and platform markers and enabled extras. Generated bytecode caches and installer bookkeeping such as local download URLs are excluded. Verification rereads the installed files and can identify a changed helper or data file even when the package version and entry module stay the same. When pip recorded the original wheel digest, `installationWheelSha256` identifies that download. A wheel installed from an index can lack this optional origin record. Strict comparison uses the installed contents in either case and includes additional files present alongside an importable package. Collection and verification use the local installation without fetching dependencies.

For a local multi-module package or editable install, declare the importable top-level roots that provide the implementation and its local imports. Each package tree includes its submodules and resource files. A single-file module is captured as a single file. Register additional data held outside those roots using `assets`. An editable install with no declared source roots produces an actionable error. A namespace split across multiple local directories can be distributed as separate wheels.

```json
{
  "extends": "components.json",
  "rules": {
    "id": "partner.local-solver",
    "version": "0.4.0",
    "entrypoint": "partner_solver.factory:build",
    "provenance": {
      "localModules": ["partner_solver", "partner_costs"],
      "assets": {"coefficients": "../private/coefficients.bin"}
    }
  }
}
```

The receipt records hashes for private model assets. Their bytes, the official input data and the native traces stay with the run owner. Store the referenced Git release, original wheels, checkpoint and raw archive at the locations listed in the experiment index.
