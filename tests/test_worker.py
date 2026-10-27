"""Supervision and isolation contracts. No model endpoints or personal Chrome."""

import io
import json
import os
import queue
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage
from jev_ultrafast.worker import (
    MAX_LINE_BYTES,
    Protocol,
    Stopped,
    read_message,
    run_task,
    usage_summary,
    validate_request,
)
from jev_ultrafast.worker_browser import OwnedChrome


@pytest.fixture(autouse=True)
def isolated_session_environment(monkeypatch):
    monkeypatch.delenv("TINK_BROWSER_SESSION_DIR", raising=False)
    monkeypatch.delenv("TINK_BROWSER_PROFILE_DIR", raising=False)


def request(**overrides):
    return validate_request({"type": "start", "protocol_version": 1, "goal": "Search for a book",
                             "start_url": "https://example.test/", **overrides})


def page():
    return {"url": "https://example.test/", "title": "Search", "text": "Search for books", "scroll": {"y": 0},
            "fingerprint": "observed-page", "actions": [
                {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 1},
            ]}


def decision(choice):
    return {"choice": choice, "operation": "TYPE_TEXT" if choice == "e1" else choice, "target": "1",
            "confidence": 1, "probabilities": {choice: 1}, "latency_ms": 1, "model": "test",
            "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.001}}


def setup_loop(monkeypatch, choices=("e1", "DONE"), responses=(True, True)):
    output = io.StringIO()
    protocol = Protocol(io.StringIO(), output)
    for index, allow in enumerate(responses, 1):
        protocol.messages.put({"type": "action_response", "id": str(index), "allow": allow})
    browser = Mock(target="source", observe=Mock(return_value=page()), fresh=Mock(return_value=True),
                   wait_for_change=Mock(return_value=page()))
    chrome = Mock()
    chrome.__enter__ = Mock(return_value=chrome)
    chrome.__exit__ = Mock(return_value=False)
    browser_factory, chrome_factory = Mock(return_value=browser), Mock(return_value=chrome)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[decision(choice) for choice in choices]))
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {
        "model": "test", "latency_ms": 1, "usage": {"prompt_tokens": 20, "completion_tokens": 2, "cost": 0.002}
    })))
    return protocol, output, browser, browser_factory, chrome_factory


@pytest.mark.parametrize("overrides", [
    {"goal": " "}, {"start_url": "file:///etc/passwd"}, {"start_url": "https://user:pass@example.test/"},
    {"start_url": "https://"}, {"max_steps": 31}, {"max_steps": True}, {"timeout_seconds": 0},
    {"timeout_seconds": float("nan")}, {"chrome_path": []}, {"protocol_version": 2},
])
def test_invalid_start_has_no_side_effects(overrides):
    with pytest.raises(ValueError):
        request(**overrides)


def test_initial_navigation_denied_before_chrome(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch, responses=(False,))
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "denied"
    assert result["evidence"] == [] and result["steps"] == []
    chrome_factory.assert_not_called()
    browser.act.assert_not_called()


def test_resume_requires_live_session_without_launch(monkeypatch):
    protocol, _, _, bf, cf = setup_loop(monkeypatch)
    result = run_task(request(start_url=None), protocol, chrome_factory=cf, browser_factory=bf)
    assert result["stop_reason"] == "browser_session_missing"
    cf.assert_not_called()


def test_unchanged_rejected_action_stops_without_spending_step_budget(monkeypatch):
    protocol, _, browser, bf, cf = setup_loop(
        monkeypatch, choices=("e1", "e1", "e1"), responses=(True, True, True, True)
    )
    browser.act.side_effect = StalePage("Covered target")
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result["status"] == "blocked"
    assert result["stop_reason"] == "action_unavailable"
    assert result["rejected_decisions"] == 3 and result["step_count"] == 0
    assert loop.choose.call_count == 3


def test_agent_does_not_own_injected_browser():
    browser = Mock(observe=Mock(return_value=page()))
    loop.Agent("https://example.test", "Read", browser=browser).close()
    browser.close.assert_not_called()
    browser.observe.side_effect = RuntimeError("Observation failed")
    with pytest.raises(RuntimeError):
        loop.Agent("https://example.test", "Read", browser=browser)
    browser.close.assert_not_called()


def test_fill_approval_contains_generated_text_before_mutation(monkeypatch):
    protocol, output, browser, browser_factory, chrome_factory = setup_loop(monkeypatch)

    def act(_action, _page, text):
        proposals = [json.loads(line) for line in output.getvalue().splitlines()
                     if json.loads(line)["type"] == "action_proposal"]
        assert proposals[-1]["kind"] == "fill"
        assert proposals[-1]["text"] == text == "book"
        assert proposals[-1]["fingerprint"] == "observed-page"

    browser.act.side_effect = act
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "completed" and result["stop_reason"] == "model_done"
    assert result["outcome_verified"] is False
    assert result["step_count"] == 1 and result["evidence"][0]["title"] == "Search"
    assert result["usage"] == {"decision_calls": 2, "text_calls": 1, "input_tokens": 40, "output_tokens": 2,
                               "cost_usd": 0.004, "cost_complete": True}
    browser.close.assert_called_once()


def test_fill_denial_prevents_browser_mutation_and_keeps_evidence(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch, responses=(True, False))
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "denied" and result["step_count"] == 0
    assert len(result["evidence"]) == 1
    browser.act.assert_not_called()
    browser.close.assert_called_once()


def test_stale_approval_does_not_authorize_retry(monkeypatch):
    protocol, output, browser, browser_factory, chrome_factory = setup_loop(
        monkeypatch, choices=("e1", "e1", "DONE"), responses=(True, True, False)
    )
    browser.act.side_effect = StalePage("Changed during approval")
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "denied" and result["step_count"] == 0
    assert browser.act.call_count == 1
    proposals = [json.loads(line) for line in output.getvalue().splitlines()
                 if json.loads(line)["type"] == "action_proposal"]
    assert [item["id"] for item in proposals] == ["1", "2", "3"]
    assert loop.field_text.call_count == 1  # Identical stale context can reuse the helper value.


def test_action_limit_stops_next_mutation(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch, choices=("e1", "e1"))
    result = run_task(request(max_steps=1), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "blocked" and result["stop_reason"] == "step_limit"
    assert browser.act.call_count == result["step_count"] == 1


def test_partial_execution_survives_observation_failure(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch)
    browser.observe.side_effect = [page(), RuntimeError("SECRET should never leave worker")]
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "error" and result["stop_reason"] == "worker_error"
    assert result["step_count"] == 1 and len(result["evidence"]) == 1
    assert "SECRET" not in json.dumps(result)


def test_model_blocked_after_action_is_not_no_progress(monkeypatch):
    protocol, _, _, browser_factory, chrome_factory = setup_loop(monkeypatch, choices=("e1", "BLOCKED"))
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["stop_reason"] == "model_blocked"


def test_protocol_rejects_wrong_approval_id_and_denies_by_default():
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.messages.put({"type": "action_response", "id": "stale", "allow": True})
    with pytest.raises(Stopped) as failure:
        protocol.approve("click", "https://example.test/", "Submit")
    assert failure.value.reason == "invalid_response"


def test_approval_wait_preserves_execution_budget_and_reports_wait(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('jev_ultrafast.worker.time', SimpleNamespace(monotonic=lambda: now[0]))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.deadline = 110

    def respond(**_kwargs):
        now[0] = 115  # Human approval took longer than the entire execution budget.
        protocol.check()
        return {'type': 'action_response', 'id': '1', 'allow': True}

    protocol.messages.get = respond
    protocol.approve('click', 'https://example.test', 'Open tickets')
    assert protocol.deadline == 125 and protocol.approval_wait_seconds == 15
    assert protocol.approval_count == 1 and protocol.approval_started is None
    now[0] = 124
    protocol.check()
    now[0] = 125
    with pytest.raises(Stopped) as failure:
        protocol.check()
    assert failure.value.reason == 'deadline_exceeded'


def test_approval_allowance_is_cumulative_and_rejects_a_late_allow(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('jev_ultrafast.worker.time', SimpleNamespace(monotonic=lambda: now[0]))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.deadline = 10

    def respond(**_kwargs):
        now[0] += 100
        return {'type': 'action_response', 'id': str(protocol.next_id), 'allow': True}

    protocol.messages.get = respond
    protocol.approve('click', 'https://example.test', 'First')
    with pytest.raises(Stopped) as failure:
        protocol.approve('click', 'https://example.test', 'Second')
    assert (failure.value.status, failure.value.reason) == ('timeout', 'approval_timeout')
    assert protocol.approval_wait_seconds == 200 and protocol.approval_count == 2
    assert protocol.approval_started is None


def test_worker_bounds_unanswered_approval_and_cancellation_wins(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('jev_ultrafast.worker.time', SimpleNamespace(monotonic=lambda: now[0]))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.deadline = 10

    def unanswered(**_kwargs):
        now[0] = 180
        raise queue.Empty()

    protocol.messages.get = unanswered
    with pytest.raises(Stopped) as failure:
        protocol.approve('click', 'https://example.test', 'Submit')
    assert failure.value.reason == 'approval_timeout'
    protocol.stop = Stopped('cancelled', 'cancelled')
    with pytest.raises(Stopped) as cancelled:
        protocol.approve('click', 'https://example.test', 'Submit')
    assert cancelled.value.reason == 'cancelled'


def test_watchdog_preserves_pending_approval_accounting(monkeypatch):
    monkeypatch.setattr('jev_ultrafast.worker.time', SimpleNamespace(monotonic=lambda: 200))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.deadline = 10
    protocol.approval_started = 0
    protocol.finished = Mock(wait=Mock(return_value=False))
    kill = Mock()
    monkeypatch.setattr('jev_ultrafast.worker.os.kill', kill)
    protocol.watch()
    assert protocol.stop.reason == 'approval_timeout'
    kill.assert_not_called()  # The bounded queue poll unwinds and records its wait.


def test_cancellation_at_approval_exit_cannot_execute_approved_action(monkeypatch):
    protocol, _, browser, bf, cf = setup_loop(monkeypatch)
    original_check = protocol.check
    interrupted = False
    kill = Mock()
    monkeypatch.setattr('jev_ultrafast.worker.os.kill', kill)

    def interleaved_check():
        nonlocal interrupted
        original_check()
        # Simulate cancellation arriving after the approval response's last
        # check, while the finally block still has to end the approval phase.
        if (protocol.next_id == 2 and protocol.approval_started is not None
                and protocol.messages.empty() and not interrupted):
            interrupted = True
            protocol.stop = Stopped('cancelled', 'cancelled')
            protocol.watch()

    monkeypatch.setattr(protocol, 'check', interleaved_check)
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert interrupted
    kill.assert_not_called()  # Watchdog leaves the bounded approval wait to unwind.
    assert result['status'] == 'cancelled' and result['stop_reason'] == 'cancelled'
    assert result['step_count'] == 0
    browser.act.assert_not_called()


@pytest.mark.parametrize('reason,status', [('approval_timeout', 'timeout'), (None, 'denied')])
def test_host_approval_timeout_is_not_a_denial(monkeypatch, reason, status):
    protocol, _, browser, bf, cf = setup_loop(monkeypatch, responses=(True,))
    protocol.messages.put({'type': 'action_response', 'id': '2', 'allow': False, 'stop_reason': reason})
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result['status'] == status
    assert result['stop_reason'] == (reason or 'approval_denied')
    assert result['approval_count'] == 1  # Exact initial navigation is auto-approved by Tink.
    assert result['elapsed_ms'] >= result['execution_ms'] >= 0
    assert result['approval_wait_ms'] >= 0
    browser.act.assert_not_called()


def test_multiple_new_tabs_preserves_submitted_click(monkeypatch):
    protocol, _, browser, bf, cf = setup_loop(monkeypatch)
    browser.observe.side_effect = [page(), RuntimeError('multiple_new_tabs')]
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result['status'] == 'blocked' and result['stop_reason'] == 'multiple_new_tabs'
    assert result['step_count'] == 1 and browser.act.call_count == 1


def test_followed_target_is_saved_and_activated_only_after_observation(monkeypatch):
    monkeypatch.setenv('TINK_BROWSER_SESSION_DIR', '/owned/session')
    protocol, _, browser, bf, cf = setup_loop(monkeypatch)
    chrome = cf.return_value
    chrome.session_data = {'browser_session': 'owned', 'target_id': 'source'}
    chrome.call.return_value = {'targetInfos': [{'targetId': 'source', 'type': 'page'}]}

    def after_click():
        assert browser.act.call_count == 1
        bf.call_args.kwargs['on_target_changed']('opened-child')
        return page()

    calls = [0]

    def observe(**_kwargs):
        calls[0] += 1
        return after_click() if calls[0] == 2 else page()

    browser.observe.side_effect = observe
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result['status'] == 'completed'
    chrome.retain_target.assert_called_once_with('opened-child')
    assert any(call.args == ('Target.activateTarget',) and call.kwargs == {'targetId': 'opened-child'}
               for call in chrome.call.call_args_list)


def test_protocol_redacts_known_credentials_before_encoding(monkeypatch):
    secret = 'test-secret-"quoted"'
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    output = io.StringIO()
    protocol = Protocol(io.StringIO(), output)
    protocol.emit("observation", text="page contains " + secret, nested={"value": secret})
    payload = json.loads(output.getvalue())
    assert payload["text"] == "page contains [redacted]"
    assert payload["nested"]["value"] == "[redacted]"


def test_protocol_bounds_lines():
    with pytest.raises(ValueError):
        read_message(io.StringIO('{"x":"' + "a" * MAX_LINE_BYTES + '"}\n'))


def test_valid_unicode_goal_fits_protocol_limit():
    payload = {"type": "start", "protocol_version": 1, "goal": "📚" * 12000,
               "start_url": "https://example.test/"}
    assert validate_request(read_message(io.StringIO(json.dumps(payload) + "\n")))["goal"] == payload["goal"]


def test_credential_material_cannot_be_approved_when_redacted(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "do-not-type-this-secret")
    output = io.StringIO()
    protocol = Protocol(io.StringIO(), output)
    with pytest.raises(Stopped) as failure:
        protocol.approve("fill", "https://example.test/", "API key", text="do-not-type-this-secret")
    assert failure.value.reason == "credential_material"
    assert output.getvalue() == ""


def test_target_close_error_does_not_mask_denial(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch, responses=(True, False))
    browser.close.side_effect = RuntimeError("Target vanished")
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "denied" and result["stop_reason"] == "approval_denied"


def test_error_cost_is_incomplete_when_provider_has_not_returned_usage(monkeypatch):
    protocol, _, _, browser_factory, chrome_factory = setup_loop(monkeypatch)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=RuntimeError("failed request")))
    result = run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    assert result["status"] == "error" and result["usage"]["cost_complete"] is False


def test_only_disposable_target_is_activated(monkeypatch):
    protocol, _, browser, browser_factory, chrome_factory = setup_loop(monkeypatch)
    run_task(request(), protocol, chrome_factory=chrome_factory, browser_factory=browser_factory)
    chrome_factory.return_value.call.assert_called_once_with("Target.activateTarget", targetId=browser.target)


def test_missing_cost_is_marked_incomplete():
    assert usage_summary({"decisions": [{"usage": {"input_tokens": 1}}]})["cost_complete"] is False


def test_self_test_is_offline_and_accepts_custom_chrome_path(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "jev_ultrafast.worker", "--self-test", "--chrome-path", str(tmp_path / "missing")],
        capture_output=True, text=True, timeout=5, check=True,
    )
    report = json.loads(result.stdout)
    assert report["protocol_version"] == 1 and report["ready"] is True
    assert report["chrome_available"] is False
    assert report["chrome_path"] is None
    assert report["approval_wait_budget"] is True and report["popup_following"] is True
    assert report["workflow_support"] is True


def test_worker_import_does_not_load_harness_or_its_workspace_env():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import jev_ultrafast.worker; "
         "print(any(name.startswith('browser_harness') for name in sys.modules))"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    assert result.stdout.strip() == "False"


def test_cancel_interrupts_pending_approval_without_chrome():
    child = subprocess.Popen([sys.executable, "-m", "jev_ultrafast.worker"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        child.stdin.write(json.dumps({"type": "start", "protocol_version": 1, **request()}) + "\n")
        child.stdin.flush()
        proposal = json.loads(child.stdout.readline())
        assert proposal["type"] == "action_proposal" and proposal["kind"] == "navigate"
        child.stdin.write('{"type":"cancel"}\n')
        child.stdin.flush()
        child.wait(timeout=5)
        result = json.loads(child.stdout.read())
        assert result["status"] == "cancelled" and result["steps"] == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("needs_termination", [False, True])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_owned_chrome_launch_is_isolated_and_cleanup_is_owned(monkeypatch, tmp_path, needs_termination, platform):
    profile = Mock(name=str(tmp_path))
    profile.name = str(tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/owned\n")
    process = Mock(pid=987, poll=Mock(return_value=None if needs_termination else 0))
    if needs_termination:
        process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 3), 0]
    socket = Mock(recv=Mock(return_value='{"id":1,"result":{}}'))
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-pass-to-chrome")
    task_tmp = str(tmp_path / ("nested-task-temp-" * 8))
    monkeypatch.setenv("TMPDIR", task_tmp)
    monkeypatch.setattr("jev_ultrafast.worker_browser.sys.platform", platform)
    monkeypatch.setattr("jev_ultrafast.worker_browser.chrome_binary", lambda _=None: "/owned/chrome")
    monkeypatch.setattr("jev_ultrafast.worker_browser.tempfile.TemporaryDirectory", Mock(return_value=profile))
    launch = Mock(return_value=process)
    monkeypatch.setattr("jev_ultrafast.worker_browser.subprocess.Popen", launch)
    monkeypatch.setattr("jev_ultrafast.worker_browser.connect", Mock(return_value=socket))
    original = deepcopy(os.environ)
    with OwnedChrome() as chrome:
        assert chrome.process is process
        command = launch.call_args.args[0]
        assert f"--user-data-dir={tmp_path}" in command
        assert "--remote-debugging-port=0" in command
        assert "start_new_session" not in launch.call_args.kwargs  # Stays in host-owned process group.
        assert "OPENROUTER_API_KEY" not in launch.call_args.kwargs["env"]
        assert launch.call_args.kwargs["env"]["TMPDIR"] == ("/tmp" if platform == "linux" else task_tmp)
        assert dict(os.environ) == dict(original)
    assert json.loads(socket.send.call_args.args[0])["method"] == "Browser.close"
    assert process.terminate.call_count == int(needs_termination)
    assert process.wait.call_count == 1 + int(needs_termination)
    profile.cleanup.assert_called_once()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_persistent_chrome_temp_path_preserves_session_ownership(monkeypatch, tmp_path, platform):
    session = tmp_path / ("nested-session-" * 8)
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(session))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "task-temp"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-pass-to-chrome")
    monkeypatch.setattr("jev_ultrafast.worker_browser.sys.platform", platform)
    monkeypatch.setattr("jev_ultrafast.worker_browser.chrome_binary", lambda _=None: "/owned/chrome")
    launch = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr("jev_ultrafast.worker_browser.subprocess.Popen", launch)
    monkeypatch.setattr(OwnedChrome, "_connect_persistent", Mock())
    original = dict(os.environ)
    with OwnedChrome() as chrome:
        session = session.resolve()
        assert chrome.session_data["profile_dir"] == str(session / "browser-data")
        assert f"--user-data-dir={session / 'browser-data'}" in launch.call_args.args[0]
        assert not {"--disable-sync", "--disable-background-networking", "--disable-component-update",
                    "--password-store=basic"}.intersection(launch.call_args.args[0])
        assert launch.call_args.kwargs["cwd"] == session
        assert launch.call_args.kwargs["start_new_session"] is True
        environment = launch.call_args.kwargs["env"]
        assert environment["TMPDIR"] == ("/tmp" if platform == "linux" else str(session / "tmp"))
        assert "OPENROUTER_API_KEY" not in environment
        assert dict(os.environ) == original


def test_password_proposal_is_masked_but_executor_receives_value(monkeypatch):
    protocol, output, browser, bf, cf = setup_loop(monkeypatch)
    browser.observe.return_value['actions'][0]['sensitive'] = True
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result['status'] == 'completed'
    proposals = [json.loads(line) for line in output.getvalue().splitlines()
                 if json.loads(line)['type'] == 'action_proposal']
    assert proposals[-1]['text'] == '[redacted password]'
    assert browser.act.call_args.kwargs['text'] == 'book'


def test_verification_handoff_continues_same_browser(monkeypatch):
    protocol, output, browser, bf, cf = setup_loop(monkeypatch, responses=(True,))
    browser.observe.return_value['authenticationRequired'] = True
    protocol.messages.put({'type': 'human_response', 'answer': '123456'})
    protocol.messages.put({'type': 'action_response', 'id': '2', 'allow': True})
    result = run_task(request(), protocol, chrome_factory=cf, browser_factory=bf)
    assert result['status'] == 'completed'
    assert cf.call_count == 1 and bf.call_count == 1
    assert output.getvalue().count('"type": "human_required"') == 1
    assert '123456' in loop.choose.call_args.args[1]


def test_agent_verification_is_bound_to_pending_tab_url_before_code_enters_goal(monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/owned/session")
    protocol, _, browser, bf, cf = setup_loop(monkeypatch)
    chrome = cf.return_value
    chrome.session_data = {"browser_session": "owned", "target_id": "source"}
    chrome.call.return_value = {"targetInfos": [{"targetId": "source", "type": "page"}]}
    browser.observe.return_value["authenticationRequired"] = True
    first = run_task(request(start_url=None, verification_handoff="agent"), protocol,
                     chrome_factory=cf, browser_factory=bf)
    assert first["stop_reason"] == "verification_required"
    assert chrome.session_data["pending_verifications"]["source"] == {
        key: first["verification"][key] for key in ("url", "requested_at")
    }
    browser.observe.return_value["url"] = "https://different.example.test/verify"
    protocol = Protocol(io.StringIO(), io.StringIO())
    second = run_task(request(start_url=None, tab_id="source", verification_response="fixture-code"), protocol,
                      chrome_factory=cf, browser_factory=bf)
    assert second["stop_reason"] == "verification_not_requested"
    browser.act.assert_not_called()
    loop.choose.assert_not_called()
    assert "fixture-code" not in json.dumps(chrome.session_data)
    assert chrome.session_data["pending_verifications"] == {}


def test_matching_verification_is_consumed_once_and_not_stored(monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/owned/session")
    protocol, _, browser, bf, cf = setup_loop(monkeypatch, choices=("DONE",))
    chrome = cf.return_value
    chrome.session_data = {"browser_session": "owned", "target_id": "source",
                          "pending_verifications": {"source": {"url": page()["url"], "requested_at": 1}}}
    chrome.call.return_value = {"targetInfos": [{"targetId": "source", "type": "page"}]}
    browser.observe.return_value["authenticationRequired"] = True
    result = run_task(request(start_url=None, tab_id="source", verification_response="fixture-code"), protocol,
                      chrome_factory=cf, browser_factory=bf)
    assert result["status"] == "completed"
    assert "fixture-code" in loop.choose.call_args.args[1]
    assert chrome.session_data["pending_verifications"] == {}
    assert "fixture-code" not in json.dumps(chrome.session_data)
    again = run_task(request(start_url=None, tab_id="source", verification_response="fixture-code"), protocol,
                     chrome_factory=cf, browser_factory=bf)
    assert again["stop_reason"] == "verification_not_requested"
    assert loop.choose.call_count == 1


@pytest.mark.parametrize("changed_origin", [False, True])
def test_verification_resume_waits_for_existing_form_to_render(monkeypatch, changed_origin):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/owned/session")
    protocol, _, browser, bf, cf = setup_loop(monkeypatch, choices=("DONE",))
    chrome = cf.return_value
    chrome.session_data = {"browser_session": "owned", "target_id": "source",
                          "pending_verifications": {"source": {"url": page()["url"], "requested_at": 1}}}
    chrome.call.return_value = {"targetInfos": [{"targetId": "source", "type": "page"}]}
    loading = {**page(), "text": "Authentication", "actions": []}
    ready = {**page(), "authenticationRequired": True}
    if changed_origin:
        ready["url"] = "https://different.example.test/verify"
    browser.observe.side_effect = [loading, loading, ready]
    result = run_task(request(start_url=None, tab_id="source", verification_response="fixture-code"), protocol,
                      chrome_factory=cf, browser_factory=bf)
    assert browser.observe.call_count == 3
    browser.act.assert_not_called()
    if changed_origin:
        assert result["stop_reason"] == "verification_not_requested"
        loop.choose.assert_not_called()
    else:
        assert result["status"] == "completed"
        assert "fixture-code" in loop.choose.call_args.args[1]
    assert "fixture-code" not in json.dumps(chrome.session_data)


def test_verification_cannot_follow_a_stale_retry_to_another_authentication_page(monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/owned/session")
    protocol, _, browser, bf, cf = setup_loop(monkeypatch, responses=(True,))
    chrome = cf.return_value
    chrome.session_data = {"browser_session": "owned", "target_id": "source",
                          "pending_verifications": {"source": {"url": page()["url"], "requested_at": 1}}}
    chrome.call.return_value = {"targetInfos": [{"targetId": "source", "type": "page"}]}
    first = {**page(), "authenticationRequired": True}
    redirected = {**first, "url": "https://different.example.test/verify", "fingerprint": "changed"}
    browser.observe.side_effect = [first, redirected]
    browser.act.side_effect = StalePage("navigation during approval")
    result = run_task(request(start_url=None, tab_id="source", verification_response="fixture-code"), protocol,
                      chrome_factory=cf, browser_factory=bf)
    assert result["stop_reason"] == "verification_not_requested"
    assert result["step_count"] == 0
    assert loop.choose.call_count == 1
    assert browser.act.call_count == 1  # Rejected before mutation; never retried on the new page.


@pytest.mark.parametrize("navigation", [True, False])
def test_navigation_or_resolved_auth_clears_pending_verification(monkeypatch, navigation):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/owned/session")
    protocol, _, browser, bf, cf = setup_loop(monkeypatch, choices=("DONE",))
    chrome = cf.return_value
    chrome.session_data = {"browser_session": "owned", "target_id": "source",
                          "pending_verifications": {"source": {"url": page()["url"], "requested_at": 1}}}
    chrome.call.return_value = {"targetInfos": [{"targetId": "source", "type": "page"}]}
    result = run_task(request(start_url=page()["url"] if navigation else None), protocol,
                      chrome_factory=cf, browser_factory=bf)
    assert result["status"] == "completed"
    assert chrome.session_data["pending_verifications"] == {}


def test_persistent_process_identity_uses_launch_nonce_not_wrapper_path(monkeypatch, tmp_path):
    chrome = OwnedChrome()
    chrome.profile_dir = tmp_path
    chrome.session_data = {'pid': 123, 'binary': '/opt/google/chrome/google-chrome',
                           'browser_session': 'a' * 32}
    command = (f'S /opt/google/chrome/chrome --user-data-dir={tmp_path} '
               f'--remote-debugging-port=0 --tink-browser-session={"a" * 32}')
    probe = Mock(return_value=Mock(returncode=0, stdout=command))
    monkeypatch.setattr('jev_ultrafast.worker_browser.subprocess.run', probe)
    assert chrome._owned_process_alive()
    probe.return_value.stdout = command.replace('a' * 32, 'b' * 32)
    with pytest.raises(RuntimeError, match='browser_session_unavailable'):
        chrome._owned_process_alive()
    probe.return_value.stdout = 'Z [chrome] <defunct>'
    assert not chrome._owned_process_alive()


def test_persistent_endpoint_must_match_owned_profile_metadata(monkeypatch, tmp_path):
    chrome = OwnedChrome()
    chrome.profile_dir = tmp_path
    chrome.session_data = {'endpoint': 'ws://127.0.0.1:1111/devtools/browser/original'}
    (tmp_path / 'DevToolsActivePort').write_text('2222\n/devtools/browser/different\n')
    connect = Mock()
    monkeypatch.setattr('jev_ultrafast.worker_browser.connect', connect)
    with pytest.raises(RuntimeError, match='browser_session_unavailable'):
        chrome._connect_persistent()
    connect.assert_not_called()


@pytest.mark.parametrize('allow_launch,has_page', [(True, False), (False, False), (True, True), (False, True)])
def test_persistent_reconnect_after_last_window_closed(monkeypatch, tmp_path, allow_launch, has_page):
    chrome = OwnedChrome(allow_launch=allow_launch)
    chrome.profile_dir = chrome.session_dir = tmp_path
    chrome.session_data = {'target_id': 'closed-tab'}
    (tmp_path / 'DevToolsActivePort').write_text('12345\n/devtools/browser/owned\n')
    monkeypatch.setattr('jev_ultrafast.worker_browser.connect', Mock())
    pages = [{'type': 'page', 'targetId': 'open-tab'}] if has_page else []

    def respond(method, **params):
        if method == 'Target.getTargets':
            return {'targetInfos': list(pages)}
        if method == 'Target.createTarget':
            assert params == {'url': 'about:blank', 'newWindow': True, 'background': True}
            pages.append({'type': 'page', 'targetId': 'new-blank-tab'})
            return {'targetId': 'new-blank-tab'}
        if method == 'Browser.setDownloadBehavior':
            # macOS can keep Chrome/CDP alive after unloading the last profile context.
            if not pages:
                raise RuntimeError('browser_request_failed')
            assert params == {'behavior': 'deny'}
        return {}

    chrome.call = Mock(side_effect=respond)
    chrome._connect_persistent()
    methods = [call.args[0] for call in chrome.call.call_args_list]
    assert methods.count('Target.createTarget') == int(allow_launch and not has_page)
    assert methods.count('Browser.setDownloadBehavior') == int(allow_launch or has_page)
    if allow_launch and not has_page:
        assert methods.index('Target.createTarget') < methods.index('Browser.setDownloadBehavior')
    # Reopening a window must not misreport the previous controlled tab as reused.
    assert chrome.session_data['target_id'] == 'closed-tab'
    assert json.loads((tmp_path / 'session.json').read_text()) == chrome.session_data


def test_reconnected_browser_requires_download_policy(monkeypatch, tmp_path):
    chrome = OwnedChrome()
    chrome.profile_dir = chrome.session_dir = tmp_path
    (tmp_path / 'DevToolsActivePort').write_text('12345\n/devtools/browser/owned\n')
    monkeypatch.setattr('jev_ultrafast.worker_browser.connect', Mock())

    def respond(method, **_params):
        if method == 'Target.getTargets':
            return {'targetInfos': []}
        if method == 'Target.createTarget':
            return {'targetId': 'new-blank-tab'}
        if method == 'Browser.setDownloadBehavior':
            raise RuntimeError('browser_request_failed')
        return {}

    chrome.call = Mock(side_effect=respond)
    with pytest.raises(RuntimeError, match='browser_request_failed'):
        chrome._connect_persistent()
    assert not (tmp_path / 'session.json').exists()


def test_unpublished_launch_is_reaped_after_interruption(monkeypatch, tmp_path):
    monkeypatch.setenv('TINK_BROWSER_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setattr('jev_ultrafast.worker_browser.chrome_binary', lambda _=None: '/owned/chrome')
    process = Mock(pid=123)
    monkeypatch.setattr('jev_ultrafast.worker_browser.subprocess.Popen', Mock(return_value=process))
    monkeypatch.setattr(OwnedChrome, '_save_session', Mock(side_effect=Stopped('cancelled', 'cancelled')))
    with pytest.raises(Stopped):
        with OwnedChrome():
            pass
    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def test_close_waits_for_exiting_process_without_signalling_unknown_pid(monkeypatch, tmp_path):
    monkeypatch.setenv('TINK_BROWSER_SESSION_DIR', str(tmp_path))
    chrome = OwnedChrome()
    chrome.session_dir = tmp_path
    chrome.socket = Mock()
    (tmp_path / 'session.json').write_text('{}')
    chrome._owned_process_alive = Mock(side_effect=[True, RuntimeError('browser_session_unavailable'), False])
    kill = Mock()
    monkeypatch.setattr('jev_ultrafast.worker_browser.os.kill', kill)
    chrome.shutdown()
    kill.assert_not_called()
    assert not (tmp_path / 'session.json').exists()


def test_close_rechecks_identity_before_signal_escalation(monkeypatch, tmp_path):
    monkeypatch.setenv('TINK_BROWSER_SESSION_DIR', str(tmp_path))
    chrome = OwnedChrome()
    chrome.session_dir = tmp_path
    chrome.socket = Mock()
    (tmp_path / 'session.json').write_text('{}')
    chrome._owned_process_alive = Mock(side_effect=[True, RuntimeError('browser_session_unavailable'), False])
    clock = Mock(monotonic=Mock(side_effect=[0, 4, 4, 4]))
    monkeypatch.setattr('jev_ultrafast.worker_browser.time', clock)
    kill = Mock()
    monkeypatch.setattr('jev_ultrafast.worker_browser.os.kill', kill)
    chrome.shutdown()
    kill.assert_not_called()
    assert not (tmp_path / 'session.json').exists()
