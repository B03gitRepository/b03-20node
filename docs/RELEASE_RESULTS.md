# Joint release acceptance

The two release profiles run the same packet work-order interfaces. The tables and CPU forward times below use the final T136 release1 receipts, with the default device queue. Wall-clock timings vary by host load. The default comparison uses three authorized services, the original requests and QoS, the same seed and the same score window per scale. Each cell describes one measured run. U is unmet service-seconds. W weights unmet seconds by business priority for evaluation. Device queries count native per-hop readbacks through the end of drain.

| Configuration | 20 nodes · U / W | Tool choices / readbacks | 100 nodes · U / W | Tool choices / readbacks |
|---|---:|---|---:|---|
| Fixed I, default components | 15.328 / 78.739 | I × 2 / 104 | 5.563 / 17.026 | I × 1 / 110 |
| Independent widest-residual rule M | 15.328 / 78.739 | M × 2 / 104 | 4.363 / 13.426 | M × 1 / 110 |
| Fixed I, alternate knowledge provider | 15.328 / 78.739 | I × 2 / 104 | 4.663 / 14.326 | I × 1 / 110 |
| Fixed I, link-change reporting adapter | 16.952 / 86.537 | I × 2 / 104 | 5.563 / 17.026 | I × 1 / 110 |
| Frozen model on CPU, dynamic INT8 | 61.655 / 322.933 | C / C / 104 | 102.000 / 306.000 | A / 0 |

The default GPU policy and the replacement frozen factory use the same matching weights and legal-action map. The README gives both commands. The archive records actual model calls, decisions, physical activations and signed readbacks.

## From delivered evidence to device readback

Rows below use the fixed-I work order. Each timing cell shows mean seconds and the number of measured cases in the scoring window. A device-change order needs all distinct writes delivered and a signed readback to enter the dispatch distribution. Per-run coverage and drain-only measurements are recorded separately in the verification JSON.

| Nodes | Perception | Generation | Dispatch | Online-pair connectivity | Served share of requested service-seconds |
|---:|---:|---:|---:|---:|---:|
| 20 | 1.115 (1) | 1.769 (4) | 2.389 (2) | 100.00% | 93.61% |
| 100 | 1.109 (1) | 0.739 (4) | 0.419 (1) | 100.00% | 94.55% |

The 100-node pure M run saved 1.200 U and 3.600 W relative to fixed I in this window. The frozen policies retain their registered action mappings. K044 was retrieved at the first delivered, authorized unmet demand in both profiles. The first three authorized open requests were already present in the rule proposal. The knowledge check verified proposal retention and rates. It added zero opening admissions.

A separately registered simulation adapter increased reporting after a physical port change. It reduced the measured two-endpoint perception time to approximately 0.20 s at both scales. Its 20-node service result is shown in the first table. Faster reports and earlier service recovery are measurable separately.

## Inference and training

The CPU measurements used four threads on an x86-64 Xeon Platinum 8352V, Ubuntu 22.04.1 LTS. All 197 quantized Linear layers executed on each call. The remaining floating-point operations are described in [the model interface](MODEL.md).
- The 20-node profile processed 2041, 2178 input tokens. Forward time was 8.64, 9.12 s. It recorded 3 signed work orders and 104 device readbacks.
- The 100-node profile processed 11138 input tokens. Forward time was 77.65 s. It recorded 0 signed work orders and 0 device readbacks.

The 100-node INT8 policy chose a hold in the original 34 s scoring window. The fixed-I row above is the CPU-only baseline that completed device writes and readback. Model loading and forward time depend on the execution host.

The online PPO updates were measured on candidate rc04 in two authorized 100-node development episodes. The final release1 verifier and a fresh process reproduced the saved policy. With one sampled decision per episode, the first update changed v374 to v375. The updated policy sampled the next episode before the second update produced v376. All 338 trainable tensors changed on each update. A fresh process reconstructed the final full parameter fingerprint, sampling cursor, next action and behavior probability. The trainer saved a compact replay checkpoint against the pinned full-parameter base. See [training and restoration](MODEL.md).

## Whole workload and historical replay

The same short windows were also run with the entire official service list. The score window remains fixed while solver, dispatch and confirmation consume the packet clock.

| Nodes | Requested service-seconds | U | W | Native readbacks through drain | Signed before score end |
|---:|---:|---:|---:|---:|---:|
| 20 | 3280 | 1649.756 | 5122.760 | 502 | 2 |
| 100 | 7480 | 7480.000 | 35802.000 | 0 | 0 |

Longer solving can move a configuration beyond the score boundary. A full-service run and a three-service work order have different workloads. Read `coverage` and `includingDrain` when interpreting either one.

The previous public component revision is linked in [the historical comparison](COMPONENT_VALIDATION.md). Replaying an original receipt on its original verifier reproduced the saved report byte for byte for both scales. The current verifier also read the earlier archives.

To reproduce the source tests, set `B03_SIMULATOR` to the provider's engine root and run `python -B -m unittest discover -s tests`. Use the [joint integration guide](JOINT_OPTIMIZATION.md) for the partner configuration matrix and the [simulation interface](SIMULATOR.md) for opt-in terminal-cleanup isolation.
