"""Supervision and isolation contracts. No model endpoints or personal Chrome."""

import io
import json
import os
import subprocess
import sys
from copy import deepcopy
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
    browser = Mock(observe=Mock(return_value=page()), fresh=Mock(return_value=True))
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


def test_owned_chrome_launch_is_isolated_and_cleanup_is_owned(monkeypatch, tmp_path):
    profile = Mock(name=str(tmp_path))
    profile.name = str(tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/owned\n")
    process = Mock(pid=987, poll=Mock(return_value=None))
    socket = Mock(recv=Mock(return_value='{"id":1,"result":{}}'))
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-pass-to-chrome")
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
        assert dict(os.environ) == dict(original)
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    profile.cleanup.assert_called_once()
