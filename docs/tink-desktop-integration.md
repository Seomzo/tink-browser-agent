# Tink desktop integration proposal

Status: this proposal guided the implemented integration. The supervised worker is
documented in [tink-worker.md](tink-worker.md), and the Tink feature branch includes
the host tool, runtime bridge, profile installer, and desktop evidence card. The
remaining live-preview and persistent-session ideas below are future work.

## Interaction

The user gives a normal instruction in Tink chat. Tink's central model plans the task, delegates a bounded browser job, receives observations and execution evidence, and decides what to do next. Jev chooses browser operations and targets inside that job. The configured text helper generates field values. Tink's central model evaluates findings and writes the user-facing answer.

An example tool contract, to be implemented:

```json
{
  "name": "browser_task",
  "arguments": {
    "start_url": "https://github.com/Seomzo?tab=repositories",
    "goal": "Inspect the public repository list and collect repository names and links.",
    "max_steps": 30,
    "timeout_seconds": 120
  }
}
```

The returned result should contain the stop reason, visited pages and captured evidence, actions, model usage, elapsed time and whether the final outcome was independently verified. A model's DONE decision is a stop reason, not verified success. Limits are clamped by the host, not controlled solely by model-provided arguments.

## What exists today

- The reference agent accepts a starting URL and natural-language goal through `Agent(url, goal)` and `examples/run.py`. The inspector currently exposes three prepared starting scenarios.
- Its operations are click, type, native select, page scroll, wait, done and blocked. It does not produce a research report or retain a complete multi-page evidence collection for a parent agent.
- It returns page state and action history; the current observation is bounded visible content. A robust research workflow needs explicit collection beyond the final snapshot.
- Frames, shadow roots, uploads, pop-up tabs and other unsupported widgets still limit workflow coverage.
- Three verified runs on the local hotel task establish initial connectivity and behavior, not reliability on GitHub or dealership software.

## Integration seam verified in Tink

The desktop reuses the Python agent through JSON-RPC/WebSocket. General plugin registration exists in `hermes_cli/plugins.py` (`PluginContext.register_tool`), with profile-local plugin discovery. Use a standalone Tink browser plugin and the existing registry, rather than vendoring this fork into the desktop renderer or replacing the existing low-level browser tools.

The standard agent path emits tool-start and tool-complete events for registered tools, so a basic tool activity card and final result can use existing desktop behavior. Per-browser-step progress is additional work: the current gateway progress handler is specialized and does not automatically forward browser worker events.

Tink also has an alternate runtime tool bridge with a selected tool allowlist. Registering a plugin alone does not expose it through every model runtime; verify and extend the active runtime bridge deliberately. Do not change a live conversation's tool list mid-turn.

Desktop tool selection and managed access policy also apply. Confirm that the configured toolsets include the plugin and that any managed entitlement permits the new tool. The integration must go through the normal dispatcher and its policy checks.

`agent/browser_provider.py` provisions browser sessions for the existing low-level tools. It is not the decision-model or autonomous-task-runner interface.

The preview pane is a separate Electron webview with its own storage partition. The current Jev worker controls a Chrome CDP target. Showing the same URL in the preview pane does not reproduce the controlled tab's session or actions. A live browser panel must explicitly connect to that owned session; a first version can use the external test Chrome with the ordinary tool card.

## First usable integration

1. Package a worker around the tested agent, in its existing Python 3.12 environment. Tink's runtime supports Python 3.11–3.13; a subprocess avoids assuming the parent environment can import the agent and its dependencies. Keep keys in the host's credential configuration and behavioral settings in its configuration file.
2. Register one experimental `browser_task` plugin tool, expose it through the chosen Tink runtime, and check availability before advertising it. Preserve existing tools and their selection behavior.
3. Give each job an owned tab/session and a host-clamped step/time budget. Capture useful page evidence during the run and return it with source URLs. Keep partial results after failure or cancellation.
4. Connect cancellation to Tink's thread-scoped interrupt mechanism. A synchronous `Agent.run()` call does not automatically observe Tink's Stop button. The wrapper must poll interruption and terminate only its owned job, including its tab, when needed.
5. Add an explicit authorization boundary before browser actions that submit or change records. Tink has a reusable approval mechanism, but approving an outer tool call does not supervise hidden inner clicks. Navigation freshness checks do not enforce business permissions. Initial supported workflows should have concrete scoped collection goals; a prompt saying "read only" is not an execution guard.
6. Exercise the real desktop path: tool discovery, invocation, successful evidence return, failure, cancellation and required approval. Then add optional per-step progress and the live session panel. Keep external Chrome as a working execution surface while that UI is developed.

Using this existing implementation means retaining its upstream license and notices. The separate `tink-browser-use` project remains the original implementation scaffold; embedding the reference agent does not turn its code or weights into newly authored material.

## GitHub project-review workflow

Tink defines the rubric first: technical depth, originality, completeness, usability/documentation and demonstrated impact, with explicit weights and explanations. Enumerate the public repositories and distinguish original projects, forks and unfinished experiments. Assess the user's contribution rather than treating a fork's entire upstream implementation as their work.

GitHub's API and repository/file tools are appropriate for enumerating projects and reading code. The browser can inspect repository pages, published demos and visible product behavior. Return URLs and observed evidence to the central model. It produces the 0–10 scores and explanations, with missing evidence stated. Stars alone and a README alone are insufficient evidence of engineering quality.

Start with a few repositories to verify collection, rubric consistency and tool handoff; then process the complete public list. This is a parent-agent workflow with browser subtasks, not a single existing Jev action or an implemented scoring feature.

## Dealership workflow progression

If the intended platform is Tekion, start with a bounded retrieval/reporting workflow in an authorized session or test environment. Tink interprets the request and business rules; the worker navigates the interface and returns observations. Add preparing a change next, followed by approved submission only after the approval and outcome-verification path is tested.

Authentication, MFA, frame-heavy screens, account/session separation and permissions must be tested against the actual application. No Tekion integration or workflow coverage has been verified yet. Prefer a supported application API when one is available for the task.

## Completion criteria

From an ordinary Tink chat, a request selects the browser tool, opens the intended session, returns evidence to the same task, stops when cancelled and follows the user's approval policy before changing records. The central agent can use the returned evidence to answer or continue. Only then is the tool integrated; opening the inspector inside Tink is not sufficient.
