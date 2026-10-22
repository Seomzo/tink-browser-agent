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
and reap that group on every exit path; it must never kill another browser. In
disposable mode, set the child's `TMPDIR` to a host-owned temporary directory and
remove it after reaping. In live-session mode Chrome launches in a separate
process group with a stable working/temporary directory, so killing the task
cannot kill its browser.

The final result contains `status` (`completed`, `blocked`, `denied`, `cancelled`,
`timeout`, or `error`), `stop_reason`, `summary`, `evidence`, `steps`, `step_count`,
`final_url`, `elapsed_ms`, `usage`, and `outcome_verified: false`. Evidence is bounded
visible page text with a source URL/title, retained across observations. Completion
is the model's stopping decision; the parent evaluates the evidence. Incomplete
or interrupted model charges are labeled with `usage.cost_complete: false`.
`persistent_session`, `session_reused`, and the opaque `browser_session` identify
live-session continuity. `rejected_decisions` counts actions rejected before
execution. Three consecutive rejections of the same choice on an unchanged page
return `blocked` with `stop_reason: action_unavailable`; submitted actions are
never replayed.

`--self-test` checks imports, protocol and Chrome availability without opening a
browser or making an API call. `--chrome-path /path/to/executable` overrides Chrome
detection for that check; a start message can supply `chrome_path` for execution.
The self-test's `persistent_session: true` advertises live-session support.

## Sessions and supported scope

Each task controls an owned Chrome tab. The host can retain the live browser
between calls within its Tink profile; otherwise Chrome is disposable.
Downloads are disabled. Explicit browser close flushes saved site data.
This first release supports macOS and Linux. The existing DOM/control
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

The live-session fixtures verify the same Chrome PID and target, `sessionStorage`
and a JavaScript global across tasks, BLOCKED and manual page changes, cancellation,
denial, and hard task-process kills. They also verify exclusive ownership, closed
target reporting, and explicit browser close. They do not use a personal browser.

The Tink app repository also includes an explicit opt-in paid smoke script that
tests its central-agent-to-worker path and independently verifies a synthetic
vehicle search. Keep that integration evidence separate from general browsing
reliability or the earlier upstream performance measurements.

## Password login and delayed pages

Visible password inputs are supported as masked text fields. Observations expose
only empty/filled status and a page-local revision for freshness checks, never
the field value. Password fills are masked in approval proposals, action history,
and recorded snapshots after the text helper supplies the value. Service API
keys remain forbidden as browser input.

The current text helper still obtains login values from the supplied task goal;
that goal is sent to hosted models. This is not a credential vault or a guarantee
that credentials supplied in chat are absent from chat storage. Do not describe
this path as local-only credential injection.

Empty initial pages get a bounded rendering wait. Before accepting BLOCKED, the
worker waits briefly for a changed observation and re-plans if the page changed.
It does not replay submitted actions. Live sessions and verification handoff are
described below. CAPTCHA, frames, and
popup-based SSO are not covered by password-field support.

Local desktop validation (2026-09-23): the patched worker filled the username,
clicked Next, filled the password, and clicked Login on Tekion. Captured evidence
then showed SMS/Email Authentication. No verification code was supplied and no
repair-order records were retrieved. This proves password-form progress, not an
end-to-end authenticated workflow or a speed advantage. The live run ended with
a generic worker error after repeated MFA observations; the subsequent change
bounds BLOCKED recovery to once per non-wait action and tells the policy not to
wait on missing MFA codes. That follow-up has offline coverage, not a repeated
live Tekion validation. Interactive verification handoff is now implemented and covered by offline tests; live validation is recorded separately.

## Live session, saved profile, and verification handoff

The host may set `TINK_BROWSER_PROFILE_DIR` to its own profile's browser-data
directory. Adding `TINK_BROWSER_SESSION_DIR` enables a live browser owned by that
Tink profile. Both paths are host-owned, never model-supplied arguments. The
session directory holds private metadata for this Chrome process and exact tab.
A nonblocking file lock rejects concurrent tasks or close requests. Reconnection
checks the process's launch nonce and dedicated profile plus the recorded loopback
DevTools endpoint; the worker never discovers or attaches to personal Chrome.

Omit `start_url` (or set it to null) to resume the retained tab without navigation.
A supplied URL explicitly navigates that tab after the initial approval. Each
goal starts a fresh Agent and observation while browser memory, tabs, and site
sessions stay live. `DONE`, `BLOCKED`, denial, timeout, and cancellation disconnect
only the task. Chrome also survives host backend restart and app quit until it is
explicitly closed or the user quits Chrome. Site sessions can still expire.

Resume without a live browser returns `browser_session_missing`; a user-closed
tab returns `browser_target_closed`. Supplying a URL can start a new browser or
tab, with `session_reused: false`. Unverifiable metadata fails closed with
`browser_session_unavailable`. A blocked result does not promise the goal was
achieved. The caller must inspect evidence and decide whether to resume.

Run `python -I -m jev_ultrafast.worker --close-browser` with the same host-owned
session/profile environment to close Chrome and retain its saved cookies. This
command needs no model API key, is idempotent when no session exists, and reports
`browser_profile_busy` while a task holds the lease. Close does not delete stored
site data. Without `TINK_BROWSER_SESSION_DIR`, the original disposable lifetime
remains; `TINK_BROWSER_PROFILE_DIR` alone saves disk data but closes Chrome after
each task.

Recognized verification-code screens emit one `human_required` event per task.
The host shows its normal user-input prompt and returns `human_response` with
`answer`: Continue after manual verification, a supplied code, or Cancel. The
worker holds the same Chrome process and makes no model calls while waiting.
Both host and worker deadlines receive a bounded 180-second handoff allowance.
Missing callbacks, cancellation, EOF, and deadline expiry terminate the task.
The flow does not configure email access or bypass CAPTCHA/MFA.
