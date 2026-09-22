# Jev through OpenRouter: initial live baseline

September 22, 2026. Jev is working through OpenRouter. This report covers a small synthetic diagnostic and one controlled browser task, not general web reliability.

## Configuration

- Decisions: `https://openrouter.ai/api/v1/systemone`, requested `typesafe/jev-1.13`, returned `typesafe/jev-1.13-20260917`.
- Text helper: `inception/mercury-2.5` through OpenRouter chat completions, reasoning disabled, maximum 1,024 output tokens.
- Browser: a separate local Chrome test profile, connected over loopback CDP through Browser Harness. The user's ordinary Chrome profile was not modified.
- The original Jev browser loop, operation/target questions, target validation and independent outcome check remain in use. The optional Laya backend was not loaded.

The local ignored `.env` holds credentials with owner-only permissions. They are not part of reports or source files. No direct TypeSafe account was used.

## Decision diagnostic

One pass over the 12 authored synthetic cases matched **11/12** expected actions. Median latency was **176 ms**, with a maximum and nearest-rank p95 of **225 ms**. There were no provider or validation errors. Reported Jev cost was **$0.000635292**.

The failed case was `already-complete`: the expected answer was DONE, but Jev chose BLOCKED with confidence 0.22. That failure remains in the evidence. A separate initial smoke request matched the expected action in 492 ms and cost $0.000063756; it is excluded from the diagnostic summary.

These are next-action decisions on fixed inputs, not browser-task completions. The earlier Laya diagnostic matched 4/12 cases per pass, with a 1,008 ms overall median across three passes. Different repetition counts and execution conditions prevent treating this as a full matched performance benchmark.

## Browser task and helper-format failure

Task: use destination search and filters to find Design stays in Lisbon with free cancellation, then open Casa Flora. Every attempt used a fresh tab. Final route and all filter settings were checked from the page independently of the model's DONE answer.

The first three attempts kept the original helper `json_object` mode: **1/3 passed**. The successful run took 11.803 seconds, including a 10.178-second helper call. The other two stopped before typing because the helper returned an invalid field value. Their early exit times are not successful-task latency measurements.

An isolated follow-up probe captured the malformed response: a valid `{"text": "Lisbon"}` object followed by a trailing Markdown fence. The executor correctly rejected it. Failed helper calls are not appended to the upstream trace, so their costs are missing and the first arm's cost total is incomplete. The original traces and probes remain saved locally.

## Strict-schema helper configuration

Mercury documents support for [strict JSON Schema output](https://docs.inceptionlabs.ai/capabilities/structured-outputs). The optional `TEXT_MODEL_RESPONSE_FORMAT=json_schema` setting now requests an object with exactly one `text` property, accepting string or null. Null remains an explicit abstention and is rejected before execution, as before. The parser and execution validation are unchanged; no model response is repaired or automatically retried after a validation failure.

Three new attempts with only the helper format changed all passed:

| Attempt | Verified outcome | Task time | Jev requests | Reported Jev USD | Reported helper USD |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | Passed | 2.060 s | 6 | 0.000700224 | 0.000038730 |
| 2 | Passed | 2.200 s | 6 | 0.000700224 | 0.000035650 |
| 3 | Passed | 2.094 s | 6 | 0.000700224 | 0.000034080 |

Median task time was **2.094 seconds**. Combined reported model cost was **$0.002209132 for these three tasks**, or **$0.000736377 per task** on this fixture. Those figures exclude the preceding diagnostic, smoke requests, probes, failed-format arm and credit-purchase fees. They are not a general average cost.

The task clock starts after initial navigation and observation and stops before independent verification. Browser setup was 43–49 ms with the browser and daemon already running. Cold browser startup is not included. The earlier helper delay and malformed responses remain relevant; three successful runs do not establish a long-run failure rate or prove the format change explains every timing difference.

## Evidence and checks

[Sanitized results](results/jev-openrouter-baseline.json) include both browser arms, the diagnostic, model versions, usage, errors, source hashes and SHA-256 hashes of the raw reports. Full reports remain in ignored `artifacts/comparison/jev-openrouter-*` folders. No benchmark data was used to change Jev's action instructions.

- Offline tests: 44 passed.
- Real browser execution/freshness checks: 21 passed, without model calls.
- Ruff, both JavaScript syntax checks, source distribution and wheel build: passed.
- Source changes remain local and uncommitted; no pull request or upstream message was sent.

The inspector has been configured for the strict-schema helper and the separate test browser. Next evaluation should broaden the tasks and repetitions before drawing reliability conclusions or choosing a Tink integration strategy.
