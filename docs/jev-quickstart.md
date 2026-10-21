# Run the Jev baseline

This checkout can run Jev through OpenRouter using an ordinary OpenRouter API key. No separate TypeSafe account is needed. Start here before changing the browser loop or testing local inference. The optional Laya backend is not loaded by these commands. See [OpenRouter's Jev access guide](https://openrouter.ai/docs/guides/community/jev).

## Obtain the keys

1. Open [OpenRouter API keys](https://openrouter.ai/settings/keys) and sign in.
2. Create an inference key, for example `tink-browser-testing`. Add credits if needed and set a small key spending limit.
3. Put that key after `TYPESAFE_API_KEY=` in the local `.env`. Set the gateway and model as shown below. The variable name is retained for compatibility; its value is an OpenRouter key with this gateway.
4. Put the same key after `TEXT_MODEL_API_KEY=` for field-value generation, or use a separate OpenRouter key if you want separate spending limits. Do not paste keys into chat.

The prepared configuration is:

```dotenv
TYPESAFE_API_KEY=<OpenRouter key>
TYPESAFE_BASE_URL=https://openrouter.ai/api
TYPESAFE_MODEL=typesafe/jev-1.13
TEXT_MODEL_API_KEY=<same or separate OpenRouter key>
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL=inception/mercury-2.5
TEXT_MODEL_REASONING=none
TEXT_MODEL_RESPONSE_FORMAT=json_schema
```

Replace the angle-bracket placeholders with your key values. The decision-only test uses `TYPESAFE_API_KEY`; browser tasks that type also use `TEXT_MODEL_API_KEY`. The `.env` file is ignored by Git and has owner-only permissions in the prepared local checkout. The browser inspector does not receive the keys.

Jev decisions go to `https://openrouter.ai/api/v1/systemone`, using state and typed questions. The client appends `/v1/systemone` to `TYPESAFE_BASE_URL`; do not put `/v1` in that base URL. Jev is not called through chat completions. The Mercury text helper uses `https://openrouter.ai/api/v1/chat/completions`. See the [System One gateway documentation](https://openrouter.ai/docs/guides/community/typesafe-sdk).

The prepared Mercury configuration requests a strict JSON schema. The original JSON-object mode sometimes returned a trailing Markdown fence and correctly failed validation. Omitting `TEXT_MODEL_RESPONSE_FORMAT` retains the original JSON-object request for baseline reproduction. Both modes retain the same executor checks; malformed, null or blank values are never typed. See [the measured results](jev-openrouter-results.md).

[OpenRouter currently lists](https://openrouter.ai/typesafe/jev-1.13) **$0.042 per million Jev input tokens; outputs are free**. Mercury has separate usage charges. Use returned costs and your OpenRouter activity to reconcile actual spending; the listed token rate is not an account subscription or minimum deposit.

Direct TypeSafe access remains an alternative. Use a TypeSafe key from [its console](https://console.typesafe.ai/keys), `TYPESAFE_BASE_URL=https://api.typesafe.ai`, and `TYPESAFE_MODEL=jev-1.13.0`. The code retains the direct TypeSafe gateway as its default when `TYPESAFE_BASE_URL` is omitted. The text helper can still use OpenRouter.

## Start locally

On this prepared machine, run these commands from the `tink-browser-agent` folder. Python and dependencies are already in `.venv`:

```bash
.venv/bin/jev
```

Open [the local inspector](http://127.0.0.1:8766). After editing `.env`, stop and restart the inspector so it loads the new credentials. Choose **Travel planner · fixture** for the first run, then **Start demo → Run automatically**. The inherited Flights example contains a fixed historical date, so it is not the first baseline task.

The inspector can open without keys. Making a decision requires the configured decision key, and typing requires the text-helper key. Chrome is controlled through Browser Harness.

The prepared local session uses a separate Chrome test profile at `artifacts/chrome-test-profile`, with its loopback debugging address saved as `BU_CDP_URL` in `.env`. It has passed all 21 browser guard checks. Keep this Chrome instance open while using the inspector. The ordinary signed-in Chrome profile has not been changed.

To restart the prepared test browser on this machine, run from the checkout:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$PWD/artifacts/chrome-test-profile" \
  --remote-debugging-port=52777 --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check about:blank
```

That port matches this machine's saved `BU_CDP_URL`. If using another port, update that variable and restart the inspector. A fresh installation can instead connect to an existing Chrome profile after its owner enables debugging:

1. Open `chrome://inspect/#remote-debugging` in Chrome.
2. Enable **Allow remote debugging for this browser instance**. This permits an approved local debugger to control that Chrome profile, including its signed-in tabs.
3. When Browser Harness connects, approve Chrome's connection prompt.

Then diagnose and check the connection with:

```bash
.venv/bin/browser-harness --doctor
.venv/bin/python scripts/check_guards.py
```

The second command opens an owned test tab and checks browser execution without model calls. It does not need a Browser Use Cloud subscription. For a dedicated test browser, pass its saved `BU_CDP_URL` in the shell environment when running these two standalone commands; the inspector and comparison runner load it from `.env` themselves.

For a fresh checkout, install Python 3.12+ and `uv`, run `uv sync`, and create `.env` from `.env.example` only if it does not already exist. The commands above then use the installed environment directly.

## Collect evidence

After configuring the OpenRouter key, run one small diagnostic first:

```bash
.venv/bin/python scripts/compare.py --suite decisions --backends jev --repeats 1 --warmups 0
```

This makes 12 paid decisions on authored synthetic cases. It does not move a browser or call the text helper. Inspect `results.json` at the output path, including errors and failed cases. Stop here if authentication, billing or response validation fails.

After both key variables and the browser connection work:

```bash
.venv/bin/python scripts/compare.py --suite browser --backends jev --repeats 3 --warmups 0
```

This runs three fresh local hotel tasks, capped at 20 decision requests per attempt. It checks the selected property and filters independently after each run. Save successes and failures, latency, usage and estimated cost together. This is a baseline on one controlled task, not proof of general web reliability. Raw reports stay under ignored `artifacts/comparison/` directories. Broaden the tasks after this baseline is working; then compare local inference using the same tasks and measurement boundaries.

Keep the configured gateway, requested model and returned model version in each benchmark report. Treat OpenRouter and direct TypeSafe runs as separate configurations; do not pool their latency or cost results. Setup instructions alone do not establish a successful live run.

The full [comparison methodology](tink-comparison.md) documents clocks, cost estimates and optional local inference.
