# Initial reproduction and local diagnostic results

September 22, 2026. This is an initial development result, not a completed Jev-versus-Laya benchmark.

Follow-up: authenticated hosted Jev diagnostics and browser runs are now available in [the OpenRouter baseline report](jev-openrouter-results.md). The results below describe the earlier local-model development pass.

Both full repositories were forked and cloned. The original Jev agent remains the default; the optional Laya backend loads pinned weights once per instance and uses the same operation/target mapping and execution guards. Implementation is on the local `tink/laya-comparison` branch. The GitHub forks retain their upstream baselines; the new implementation has not been committed or pushed, and no upstream contribution has been submitted.

## What was actually run

| Check | Result |
| --- | --- |
| Untouched Jev checkout, original offline tests | 31 passed |
| Tink comparison fork, offline tests | 38 passed with the Laya extra installed |
| Laya upstream CI check scripts | All seven passed: router, criteria, download, shortlist, decision_model, packaging, email |
| Ruff | Passed |
| Original frontend and snapshot JavaScript syntax | Passed |
| Package source distribution and wheel | Built successfully |
| Live hosted Jev calls | Not run: TypeSafe access is not yet available |
| Complete browser tasks with either backend | Not run in this pass: live comparison also needs the configured text-helper key and Chrome connection |

## Real Laya inference on the target Mac

Hardware was an Apple M2 with 24 GiB memory. The actual inference device was MPS, using upstream's float32 path. The English checkpoint and model revision are pinned in `UPSTREAMS.md`. Settings were unchanged: `max_len=512`, `head_max_len=192`. The wrapper reports input truncation but does not alter inputs.

The suite contains **12 authored synthetic next-action cases**, repeated three times with one resident model. These are not 36 independent tasks, and they do not measure real browser completion. One explicit warm-up call is excluded from the measured summary. Decisions are timed through the normal Jev request builder and Laya wrapper, including input audit and inference.

| Pass | Expected actions matched | Median decision time |
| --- | ---: | ---: |
| 1 | 4 / 12 | 1,067.5 ms |
| 2 | 4 / 12 | 981.5 ms |
| 3 | 4 / 12 | 1,022.0 ms |
| Overall | 12 / 36, 33.3% | 1,008.0 ms |

Overall p95 was 1,778 ms. All calls returned structurally valid choices; there were zero runtime errors. Outcomes were consistent across repeats. The cached-download run spent 54.621 seconds loading/building the model, excluded from the decision timing. An earlier one-pass run took 183.619 seconds including initial acquisition/build and had a 1,593 ms median; those timings are retained separately rather than mixed into the repeated-run summary.

Successful diagnostics were search submission, native SELECT, unsupported-operation BLOCKED and scrolling. Failures included clicking Search instead of filling an empty destination, selecting the wrong autocomplete/result, toggling an already enabled checkbox, leaving a completed page, and choosing DONE while results were loading.

The input audit found truncated instruction text in every measured case. The 20-target case also shortened all target descriptions and truncated the state. This identifies one integration defect to address, but does not prove truncation explains every wrong decision. Domain suitability, state representation and checkpoint training also need evaluation.

Shareable evidence: [sanitized per-case results and source hashes](results/laya-mps-diagnostic.json). Full local traces remain under `artifacts/comparison/laya-raw-mps-repeated/results.json`; the shareable file includes their SHA-256. The first exploratory pass is retained under `artifacts/comparison/laya-raw-mps/results.json`. These inputs contain only synthetic fixture data. No credentials or real browsing history were used, and no paid inference calls were made.

## Interpretation and next experiment

The unchanged Laya checkpoint is not an acceptable automatic replacement on these examples. Its local latency is also far from the advertised single-question T4 number. This does not establish that hosted Jev is more accurate on the same examples: that arm is still pending. It also does not settle the performance of a browser-specialized, compact-input, quantized or differently accelerated Laya model.

Once TypeSafe access is available, run the identical decision suite through Jev and then run both against the independently verified browser fixture with the same text helper. Use the baseline results to decide where Laya input adaptation or training is worthwhile. The comparison runner exposes larger input budgets as a controlled experiment; larger budgets have not been measured here and are not claimed as a fix.

A possible later upstream contribution is better input-overflow visibility. Another candidate is reducing checkpoint startup overhead: sampling the first load showed substantial time in parameter initialization before checkpoint loading. Any optimization must preserve derived buffers and inference parity before it is proposed. Neither idea has been implemented in the Laya fork or presented to its maintainers.

Tink desktop/mobile integration remains a later step, conditional on verified results. Hosted Jev stays the usable baseline; local Laya remains experimental.
