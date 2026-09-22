# Tink browser-agent comparison

This fork preserves Browser Use's Jev Ultrafast browser loop and adds an optional resident Laya decision backend. Tink integration comes after a measured comparison. Hosted Jev can use OpenRouter or the direct TypeSafe API; the prepared configuration uses OpenRouter. The code retains the direct TypeSafe gateway as its default when no gateway is configured. The local backend does not call either hosted decision endpoint; the browser agent still uses its configured text helper for field values.

Upstream versions are recorded in [UPSTREAMS.md](../UPSTREAMS.md). The Python package name remains `jev-ultrafast` while the comparison is developed. It has not been published as a separate PyPI package.

Start with [Jev credentials and the hosted baseline](jev-quickstart.md). The comparison command now defaults to Jev only; pass `--backends laya` or `--backends jev laya` explicitly for local inference. The independently authored Tink browser-use scaffold is a separate project, not a relabeling of this fork.

The first authenticated OpenRouter runs are recorded in [Jev baseline results](jev-openrouter-results.md), including the original helper-format failures and the separately measured strict-schema configuration. Record `TEXT_MODEL_RESPONSE_FORMAT` when comparing runs; omitting it reproduces the original JSON-object helper request.

## Cost of hosted Jev

Checked September 22, 2026: [TypeSafe](https://docs.typesafe.ai/models) lists **$0.042 per million input tokens, with free output tokens** for `jev-1.13.0`. [OpenRouter](https://openrouter.ai/typesafe/jev-1.13) lists the same rate for `typesafe/jev-1.13`.

`estimated Jev USD = reported input_tokens × 0.042 / 1,000,000`

Repricing the three optimized Flights runs in the upstream [raw measurements](full-speed-measurement.json):

| Run | Input tokens | Estimated Jev USD | Reported text-helper USD | Combined model USD |
| --- | ---: | ---: | ---: | ---: |
| 1 | 84,650 | 0.003555300 | 0.000055230 | 0.003610530 |
| 2 | 88,844 | 0.003731448 | 0.000120700 | 0.003852148 |
| 3 | 82,661 | 0.003471762 | 0.000057390 | 0.003529152 |
| Mean | 85,385 | 0.003586170 | 0.000077773 | 0.003663943 |

This sample is roughly **0.36 cents in Jev charges per browser task**, or **$3.59 per 1,000 similar tasks**. Including the reported text helper gives about **$3.66 per 1,000**. This is a three-run sample of one task, not an average over general browser work or an invoice. Browser hosting, local hardware/electricity and Tink's parent agent are excluded. Failed attempts, longer goals, more steps and larger page inputs change costs. Warm-up API calls in our comparison are retained in raw rows but excluded from measured-run summaries; include them when reconciling a bill.

The table reprices upstream measurements; it is not evidence of an OpenRouter run. For new OpenRouter runs, preserve returned `usage.cost` and compare it with account activity. Keep reported charges distinct from estimates when a response omits cost.

## Install and credentials

Python 3.12+ is required. Install the ordinary upstream agent with `uv sync`; add local inference with:

```bash
uv sync --extra laya
cp .env.example .env  # only if .env does not already exist
```

The prepared checkout uses the following OpenRouter configuration in its ignored `.env`:

```dotenv
TYPESAFE_BASE_URL=https://openrouter.ai/api
TYPESAFE_MODEL=typesafe/jev-1.13
TYPESAFE_API_KEY=<OpenRouter key>
TEXT_MODEL_API_KEY=<same or separate OpenRouter key>
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL=inception/mercury-2.5
TEXT_MODEL_REASONING=none
```

Jev uses `/api/v1/systemone`; Mercury uses `/api/v1/chat/completions` to generate field text. The decision client appends `/v1/systemone` to its base URL. No separate TypeSafe account is required for OpenRouter. Follow the [key setup instructions](jev-quickstart.md) and [official System One gateway guide](https://openrouter.ai/docs/guides/community/typesafe-sdk).

For direct TypeSafe access, set `TYPESAFE_BASE_URL=https://api.typesafe.ai`, `TYPESAFE_MODEL=jev-1.13.0`, and a TypeSafe key. Do not commit keys or put them in benchmark reports. The local decision-only suite requires neither key.

Laya weights are downloaded once from a pinned Hugging Face revision. A `LayaBackend` instance loads once and stays resident across requests and browser tasks. Load time is reported separately. Model caches and raw runs are not committed. CPU, Apple MPS and CUDA are selectable; the trace reports the actual device, including upstream CPU fallback. A T4 latency claim is not a Mac performance claim.

## Run the decision diagnostic

```bash
# Twelve authored synthetic examples. No browser or text helper; no paid calls.
USE_TF=0 uv run --extra laya python scripts/compare.py \
  --suite decisions --backends laya --device mps --repeats 3

# Same examples through the hosted API and the local backend; real Jev charges.
USE_TF=0 uv run --extra laya python scripts/compare.py \
  --suite decisions --backends jev laya --device mps --repeats 3

# Controlled experiment with larger Laya input budgets; no retraining is implied.
USE_TF=0 uv run --extra laya python scripts/compare.py \
  --suite decisions --backends laya --device mps --repeats 3 \
  --max-len 2048 --head-max-len 1024
```

The cases in `benchmarks/decisions.json` cover filling a field, submission, autocomplete, checkbox state, native SELECT, target choice, DONE, loading, BLOCKED, scrolling and a larger target set. They are synthetic next-action diagnostics, not a task-success benchmark or held-out generalization set. They contain expected labels solely for evaluation; the policy never receives those labels. No field text is generated or browser action executed by this suite.

Laya retains upstream input construction. Each result includes an audit of shortened instructions, shortened/dropped options and state truncation. Truncation is reported, not silently claimed to be fixed. The wrapper's reported decision time includes this audit and inference. Increasing budgets is an experimental arm and may increase latency or hurt accuracy. Do not compare its speed with a smaller-context run without recording the settings.

## Run the actual browser comparison

```bash
uv run browser-harness --doctor
USE_TF=0 uv run --extra laya python scripts/compare.py \
  --suite browser --backends jev laya --device mps --repeats 3
```

This explicitly makes paid model calls and runs Chrome. It starts a loopback-only server for the original local hotel fixture, creates a fresh tab per attempt and alternates backend order across repeats. Both arms use the same goal, browser executor, viewport and text helper. The Laya instance persists across runs. Independent verification reads the final page again and checks the Casa Flora route and all applied filters. Model DONE alone cannot pass the run. The default comparison cap is 20 decision requests per attempt; the upstream mutation guards and action budget remain in place.

The task clock starts after initial navigation/observation and ends before independent verification, matching the original measurement convention. Browser setup and model loading are separately reported. Raw traces include wrong decisions, failures, usage, source hashes, model versions and input audits. Record the configured gateway, requested model and returned model version with every benchmark. OpenRouter and direct TypeSafe are separate hosted configurations; compare them separately rather than pooling results. The summary includes pass rate, median/p95 latency and model cost per successful attempt, charging failed measured attempts against that denominator. Missing provider costs and errors mark cost information incomplete. Local compute is unpriced, not free. Use explicit `--output artifacts/...` names for experiments you plan to compare; existing result folders are never overwritten.

The local fixture is one task. After both arms run, broaden to held-out sites and varied tasks before making reliability or speed claims. Keep a separate tuning set for improvements to Laya.

## Use the resident backend in code

```python
from jev_ultrafast import Agent
from jev_ultrafast.laya_backend import LayaBackend

backend = LayaBackend(device="mps")  # one load for the process
with Agent(url, goal, decision_model=backend) as agent:
    for state in agent.run():
        report_progress(state)

# Reuse backend for subsequent tasks. Omit decision_model to use original Jev.
```

The existing library is synchronous. The current comparison runs one task at a time; a Tink worker should explicitly own and serialize its browser jobs and cancellation. The mobile client should start/monitor that same desktop-owned job. No Tink desktop, gateway or mobile code is changed in this fork.

## Contribution path

Keep benchmark evidence and browser-specific integration in this repository. A general Laya input-budget, calibration or runtime improvement should be isolated with a small regression test on the Laya fork before proposing it upstream. No upstream issue, message or pull request has been sent. The original author licenses and notices are retained.

## Validation

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline and never require model weights or API keys. Actual inference and live browser comparisons are explicit commands above. Results and remaining limitations from this development pass are recorded in `docs/tink-results.md`.
