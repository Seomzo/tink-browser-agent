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
owned process group. Execution time pauses while awaiting action approval, with
a separate cumulative 180-second approval allowance per task. Exhausting it
returns `timeout` / `approval_timeout`, and a late response cannot execute input.
The host can report the same outcome with an `action_response` containing
`allow: false` and `stop_reason: "approval_timeout"`. Ordinary `allow: false`
still means denial. The worker enforces deadlines during blocking model calls
and approval waits. The host must start the worker with a new process group
and reap that group on every exit path; it must never kill another browser. In
disposable mode, set the child's `TMPDIR` to a host-owned temporary directory and
remove it after reaping. In live-session mode Chrome launches in a separate
process group with a stable working/temporary directory, so killing the task
cannot kill its browser.

The final result contains `status` (`completed`, `blocked`, `denied`, `cancelled`,
`timeout`, or `error`), `stop_reason`, `summary`, `evidence`, `steps`, `step_count`,
`final_url`, `elapsed_ms`, `execution_ms`, `approval_wait_ms`, `approval_count`,
`usage`, and `outcome_verified: false`. `execution_ms` excludes approval waiting;
the optional legacy human-input handoff remains included. `approval_count` excludes the
exact initial URL proposal automatically approved by Tink; its brief round-trip
still counts toward waiting time. Evidence is bounded
visible page text with a source URL/title, retained across observations. Completion
is the model's stopping decision or one requested observed action; the parent evaluates the evidence. Incomplete
or interrupted model charges are labeled with `usage.cost_complete: false`.
`persistent_session`, `session_reused`, and the opaque `browser_session` identify
live-session continuity. `rejected_decisions` counts actions rejected before
execution. Three consecutive rejections of the same choice on an unchanged page
return `blocked` with `stop_reason: action_unavailable`; submitted actions are
never replayed.

Results also include observed `tabs`, `current_tab_id`, a pending `verification`
record when a code is needed, and completed `downloads`. A download entry contains
`path`, friendly `name`, `source_url`, `bytes`, and `sha256`. A verified file is
evidence of file delivery, not independent proof that the requested report or
records are correct; `outcome_verified` remains false.

`--self-test` checks imports, protocol and Chrome availability without opening a
browser or making an API call. `--chrome-path /path/to/executable` overrides Chrome
detection for that check; a start message can supply `chrome_path` for execution.
The self-test's `persistent_session: true` advertises live-session support.
`approval_wait_budget: true` and `popup_following: true` let hosts reject older
workers before relying on separate approval timing or click-opened tabs.

### Perform an already observed action

The self-test's `observed_actions: true` advertises the optional `perform` request.
When a persistent task returns page controls in `available_actions`, supported
click, native-select, keyboard and Back entries include an opaque `action_ref`.
The parent can choose one directly without asking Jev to make the same decision:

```json
{"type":"start","protocol_version":1,"action":"perform","tab_id":"observed-tab-id","action_ref":"64-lowercase-hex-characters-from-available_actions"}
```

`goal` is optional descriptive context. `start_url`, `new_tab`, and
`verification_response` are incompatible with `perform`. Text entry remains a
normal task; references cannot supply text, selectors, coordinates or new actions.
Tab inventory requests do not create a page observation or references.

The worker binds references to the latest observation's owned browser session,
tab, document, field state, target semantics and freshness guard. Private session
metadata stores random references and hashes, without raw page or field values.
Under the exclusive profile lease, the observation is consumed before approval
or execution. Approval, cancellation, popup following, MFA handoff and verified
downloads still apply. A stale, foreign or replayed reference executes nothing
and returns `blocked` / `action_ref_unavailable` with refreshed controls where
available. MFA pages do not expose action references.

A successful request executes at most one observed action and returns
`completed` / `action_performed`, updated evidence and new references. It makes
zero model calls; `usage` reports zero model tokens/cost, and an interrupted
perform request does not imply unknown model charges. `outcome_verified` remains
false: executing a control is not independent proof of the user's whole goal.

## Sessions and supported scope

Each task controls an owned Chrome tab. The host can retain the live browser
between calls within its Tink profile; otherwise Chrome is disposable.
Explicit browser close flushes saved site data. Downloads are enabled only within
a host-configured task scope, as described below.
This release supports macOS and Linux. After an approved click, the worker can
follow one newly opened page whose CDP opener is the controlled tab. It excludes
preexisting and unrelated tabs, checks for up to one second, and gives a found
tab 100 ms for siblings to appear before selecting it. Multiple matching tabs
return `blocked` / `multiple_new_tabs`; no click is replayed. The original tab
stays open and the new target becomes the profile's retained tab. A host can also
list owned tabs and explicitly choose an observed target. The bounded automatic
popup check does not handle late tabs or missing opener relationships by guessing.
Frames, shadow roots, uploads and native browser/OS dialogs remain unsupported.

Snapshots include bounded nearby visible text and observed HTTP(S) link
destinations to distinguish repeated labels. Snapshot inclusion and execution
share the same click-point resolver: it may choose an uncovered point on a card,
but never a nested interactive control. Guard identities and context are rechecked
after approval.

Keyboard support is limited to observed Escape for a visible page dialog/menu and
Enter for a supported focused input. Back is bound to the observed current and
previous browser-history entries. Changed focus, overlay, DOM identity or history
invalidates the action before input. This does not add arbitrary keys, selectors,
native password-manager unlocks, or full computer control.

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
native browser or OS authentication prompts are not covered by password-field
support. Owned-tab selection enables an email-code round trip, but does not
guarantee arbitrary SSO flows or Gmail accessibility.

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

For persistent sessions, the host can send `action: "tabs"` to obtain the owned
browser's page IDs, titles and URLs without model inference. A run may use an
observed `tab_id` to resume that exact page. Alternatively, `new_tab: true` with
`start_url` opens a separate tab and preserves the previous page. `new_tab` and
`tab_id` cannot be combined. Only pages in this owned Chrome session are eligible;
an absent target is rejected rather than replaced with a guessed tab. A task still
starts with a fresh Agent, so the host must supply the goal and relevant context.

Resume without a live browser returns `browser_session_missing`; a user-closed
tab returns `browser_target_closed`. Supplying a URL can start a new browser or
tab, with `session_reused: false`. On macOS, closing every window can leave Chrome
running with its browser context unloaded. A supplied URL reopens a blank window
in that same process and saved profile, restores the download policy, then
navigates. Resuming without a URL and explicitly closing the browser still work
in this empty-window state. Unverifiable metadata fails closed with
`browser_session_unavailable`. A blocked result does not promise the goal was
achieved. The caller must inspect evidence and decide whether to resume.

Run `python -I -m jev_ultrafast.worker --close-browser` with the same host-owned
session/profile environment to close Chrome and retain its saved cookies. This
command needs no model API key, is idempotent when no session exists, and reports
`browser_profile_busy` while a task holds the lease. Close does not delete stored
site data. Without `TINK_BROWSER_SESSION_DIR`, the original disposable lifetime
remains; `TINK_BROWSER_PROFILE_DIR` alone saves disk data but closes Chrome after
each task.

Tink uses `verification_handoff: "agent"`. A recognized verification-code page
returns `blocked` / `verification_required` with its tab ID, URL and request time.
The worker ends the bounded task while retaining the live session. The central
agent can read a fresh code through authorized email access in another owned tab
or an existing email connection, or ask the user. It resumes the pending tab using
`tab_id` and `verification_response`, without `start_url`. A response is checked
against the pending verification page and consumed for that continuation; it must
not be guessed, reused for another page, or treated as a permanent credential.
This removes the blocking human prompt from Tink's email-code round trip.

Direct clients may still choose `verification_handoff: "human"` (the protocol
default). That mode emits one `human_required` event per task and waits for
`human_response` with Continue, a code, or Cancel. It holds the session without
model calls and has a bounded 180-second allowance. Tink does not use this mode
for automated email retrieval. Neither mode configures Gmail access or bypasses
CAPTCHA/MFA, and iframe-based code fields remain unsupported.

## Manual browser sign-in

`python -I -m jev_ultrafast.browser_setup` opens normal visible Chrome with the
same host-owned profile. It first closes an idle automated session while retaining
the profile leases. The manual window has no remote-debugging endpoint, headless
flag, disabled-sync flag or basic password-store override. The user signs into
Chrome and their sites using Chrome's normal account and password-manager UI.

The helper reports `running`, `idle`, `busy` or `error`; `--status` only checks
setup state. `idle` means the setup browser is closed, not that any account is
signed in. The helper keeps exclusive ownership while normal Chrome is open,
including across a host restart; automation resumes after the user quits that
Chrome instance. If ownership cannot be verified, it fails closed.

In Tink desktop, use Settings → Advanced → **Set up browser sign-in…**, sign into
Chrome if wanted, separately confirm Gmail and the intended site account, then
quit the setup browser. Chrome sync, OS keychain unlocks, Touch ID and account
consent remain under the user's control. This is not a Gmail OAuth integration,
an exported password vault, or access to the user's everyday Chrome profile.

## Report downloads

The host supplies `TINK_BROWSER_DOWNLOAD_DIR` under the selected profile. Each
task gets a private subdirectory. Chrome saves files using its download IDs so
duplicate names cannot overwrite one another; the manifest retains the suggested
human-readable name separately. Approved page actions may initiate a download;
the model cannot supply a filesystem path or execute the downloaded file.

The tracker consumes Chrome download events, waits for in-flight transfers before
model completion within the task deadline, and verifies a regular contained file,
its received byte count and SHA-256. Symlinks, traversal, incomplete/cancelled files
and inconsistent sizes do not enter the completed manifest. The Tink host checks
containment, size and hash again before exposing the path to the central agent.
Task cleanup disables further automated downloads and cancels active transfers.
Failure reasons distinguish `download_failed`, `download_verification_failed`
and `download_cleanup_failed`; partial page evidence is retained where available.

A page label such as “Download complete” is not proof of file delivery. Browser
print/save dialogs, uploads, frame-only controls and downloads that begin outside
the bounded task remain outside this capability. Completed report contents still
need to be checked against the user's requested records.

## Current controlled checks

The workflow regression fixtures cover duplicated card labels and nested controls,
stale keyboard/history actions, owned-tab selection, verification continuation,
download completion and cancellation, and exclusive manual sign-in setup. These
are local fixtures with mocked inference, not live Google/Tekion acceptance or a
speed benchmark. Keep actual test execution counts, paid runs and live account
outcomes in the release validation record rather than inferring them from fixture
coverage. Native password-manager unlocks have not been automated.

Offline clock tests cover approval waits longer than the execution budget,
cumulative allowance exhaustion, explicit denial, cancellation, and late approval.
Owned-Chrome fixtures verify immediate and delayed child tabs, exclusion of old
and unrelated tabs, one executed click, shared cookies, retained child-page memory,
and follow-up tasks without navigation. These tests make no model API calls.
