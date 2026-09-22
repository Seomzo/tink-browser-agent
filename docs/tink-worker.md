# Supervised Tink browser worker

The Tink desktop integration runs `python -I -u -m jev_ultrafast.worker` in a
separate Python environment. It uses the same tested Jev decisions and strict
text helper as this fork, with an owned Chrome connection and action supervision.
The default demo still uses Browser Harness. This worker connects directly to its
own Chrome process and does not attach to a personal browser or harness daemon.

Tink installs a reviewed Git commit, resolves the active profile's OpenRouter key,
and sends model settings through the child environment. The worker never loads
this repository's `.env`. Tink's normal tool dispatcher, managed policy and
approval mechanisms remain in the execution path.

## Protocol version 1

Stdin and stdout carry one JSON object per line. Stdout contains only protocol
messages. The first host message is:

```json
{"type":"start","protocol_version":1,"start_url":"https://example.com","goal":"Find the contact page","max_steps":20,"timeout_seconds":120}
```

The worker emits `started`, `observation`, `action_proposal`, `step`, then `result`.
Every event includes `protocol_version: 1`. An action proposal contains an `id`,
`kind`, current `url`, observed `label`, `fingerprint`, and optional generated
`text`. The worker waits before performing navigation or the proposed action:

```json
{"type":"action_response","id":"1","allow":true}
```

Denial ends the task. The host can send `{"type":"cancel"}` or terminate the
owned process group. The worker also enforces a deadline during blocking model
calls and approval waits. The host must start the worker with a new process group
and reap that group on every exit path; it must never kill another browser. Set
the child's `TMPDIR` to a host-owned temporary directory and remove it after
reaping, so Chrome data is removed even when forced termination skips cleanup.

The final result contains `status` (`completed`, `blocked`, `denied`, `cancelled`,
`timeout`, or `error`), `stop_reason`, `summary`, `evidence`, `steps`, `step_count`,
`final_url`, `elapsed_ms`, `usage`, and `outcome_verified: false`. Evidence is bounded
visible page text with a source URL/title, retained across observations. Completion
is the model's stopping decision; the parent evaluates the evidence. Incomplete
or interrupted model charges are labeled with `usage.cost_complete: false`.

`--self-test` checks imports, protocol and Chrome availability without opening a
browser or making an API call. `--chrome-path /path/to/executable` overrides Chrome
detection for that check; a start message can supply `chrome_path` for execution.

## Sessions and supported scope

Each job gets a disposable Chrome profile and a visible controlled tab. No cookies
or saved logins are inherited, downloads are disabled, and the browser closes on
completion. This first release supports macOS and Linux. The existing DOM/control
limitations still apply, including frames, shadow roots, popups and uploads.

This is the attributed Jev fork with MIT notices preserved. It is not the original
Tink rewrite, a locally trained model, a Laya speedup, or full computer control.

## Verification

Normal `uv run pytest` is offline. Worker tests cover approval ordering, denial,
freshness after approval, bounded execution, partial evidence, protocol validation
and configured-secret redaction. Optional tests launch an isolated Chrome and use
mocked model decisions, exercising actual DOM outcomes and process cleanup:

```sh
TINK_TEST_OWNED_CHROME=1 uv run pytest tests/test_worker.py tests/test_worker_chrome.py
```

The Tink app repository also includes an explicit opt-in paid smoke script that
tests its central-agent-to-worker path and independently verifies a synthetic
vehicle search. Keep that integration evidence separate from general browsing
reliability or the earlier upstream performance measurements.
