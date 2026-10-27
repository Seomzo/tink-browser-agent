"""A pending MFA challenge permits one supervised code-request decision, never a guessed code."""
import copy
import io
import json
from unittest.mock import Mock

import pytest

import jev_ultrafast.agent as loop
from jev_ultrafast.browser import StalePage
from jev_ultrafast.questions import VERIFICATION_REQUEST
from jev_ultrafast.worker import Protocol, run_task, validate_request


@pytest.fixture
def challenge(monkeypatch, tmp_path):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.delenv("TINK_BROWSER_DOWNLOAD_DIR", raising=False)
    page = {
        "url": "https://account.example.test/verify", "title": "Verification",
        "text": "Enter the emailed code. Send another code", "scroll": {"y": 0},
        "fingerprint": "verification-form", "authenticationRequired": True,
        "actions": [
            {"id": "code", "node": 1, "kind": "fill", "role": "textbox", "label": "Code", "value": ""},
            {"id": "resend", "node": 2, "kind": "click", "role": "button", "label": "Send another code"},
            {"id": "enter", "node": 1, "kind": "key", "key": "Enter", "role": "keyboard", "label": "Enter"},
            {"id": "back", "kind": "back", "role": "navigation", "label": "Back"},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    browser = Mock(target="challenge-tab", observe=Mock(return_value=page), fresh=Mock(return_value=True))
    chrome = Mock()
    chrome.__enter__ = Mock(return_value=chrome)
    chrome.__exit__ = Mock(return_value=False)
    chrome.session_data = {
        "browser_session": "owned", "target_id": browser.target,
        "pending_verifications": {browser.target: {"url": page["url"], "requested_at": 1.0}},
    }
    chrome.call.return_value = {"targetInfos": [{"targetId": browser.target, "type": "page"}]}
    choose = Mock()
    text_helper = Mock(side_effect=AssertionError("A code-request task must never generate text"))
    monkeypatch.setattr(loop, "choose", choose)
    monkeypatch.setattr(loop, "field_text", text_helper)

    def run(choice="resend", goal="Request a fresh email code", allow=True):
        choose.return_value = {
            "choice": choice, "operation": "CLICK", "target": "2", "confidence": 1,
            "probabilities": {choice: 1}, "latency_ms": 1, "model": "fixture", "usage": {},
        }
        output = io.StringIO()
        protocol = Protocol(io.StringIO(), output)
        protocol.messages.put({"type": "action_response", "id": "1", "allow": allow})
        request = validate_request({
            "type": "start", "protocol_version": 1, "goal": goal,
            "tab_id": browser.target, "verification_handoff": "agent",
        })
        result = run_task(request, protocol, chrome_factory=Mock(return_value=chrome),
                          browser_factory=Mock(return_value=browser))
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    return page, browser, chrome, choose, text_helper, run


def test_first_verification_encounter_hands_off_without_a_model_call(challenge):
    _, browser, chrome, choose, helper, run = challenge
    chrome.session_data["pending_verifications"] = {}
    result, events = run()
    assert result["stop_reason"] == "verification_required"
    assert result["step_count"] == 0
    choose.assert_not_called()
    helper.assert_not_called()
    browser.act.assert_not_called()
    assert not any(event["type"] == "action_proposal" for event in events)


@pytest.mark.parametrize("goal", ["Request a fresh email code", "Please have the site email me a new sign-in code."])
def test_explicit_new_code_goal_gets_one_approved_observed_click_then_handoff(challenge, goal):
    page, browser, chrome, choose, helper, run = challenge
    result, events = run(goal=goal)
    assert result["stop_reason"] == "verification_required"
    assert result["step_count"] == 1
    assert result["verification"]["tab_id"] == browser.target
    assert result["verification"]["url"] == page["url"]
    assert chrome.session_data["pending_verifications"][browser.target]["url"] == page["url"]
    assert choose.call_count == browser.act.call_count == 1
    assert choose.call_args.args[1] == goal + "\n\n" + VERIFICATION_REQUEST
    assert browser.act.call_args.args[0] == page["actions"][1]
    helper.assert_not_called()
    proposals = [event for event in events if event["type"] == "action_proposal"]
    assert len(proposals) == 1 and proposals[0]["label"] == "Send another code"
    assert not any(event["type"] == "human_required" for event in events)


def test_resend_waits_for_same_pending_form_after_heading_only_first_observation(challenge):
    page, browser, chrome, choose, helper, run = challenge
    loading = {**page, "text": "Authentication", "actions": [], "authenticationRequired": False,
               "fingerprint": "heading-only"}
    browser.observe.side_effect = [loading, page, copy.deepcopy(page)]
    result, events = run()
    assert result["stop_reason"] == "verification_required"
    assert result["step_count"] == 1
    assert browser.observe.call_count == 3
    assert choose.call_count == browser.act.call_count == 1
    assert browser.act.call_args.args[0] == page["actions"][1]
    assert chrome.session_data["pending_verifications"][browser.target]["url"] == page["url"]
    assert VERIFICATION_REQUEST in choose.call_args.args[1]
    assert len([event for event in events if event["type"] == "action_proposal"]) == 1
    helper.assert_not_called()


@pytest.mark.parametrize("choice", ["code", "enter", "back", "wait", "DONE", "BLOCKED", "unobserved"])
def test_nonclick_decisions_handoff_before_any_input_or_text_generation(challenge, choice):
    _, browser, chrome, choose, helper, run = challenge
    result, events = run(choice)
    assert result["stop_reason"] == "verification_required"
    assert result["step_count"] == 0
    assert browser.target in chrome.session_data["pending_verifications"]
    assert choose.call_count == 1
    browser.act.assert_not_called()
    helper.assert_not_called()
    assert not any(event["type"] == "action_proposal" for event in events)


def test_goal_without_code_request_can_only_handoff_when_policy_selects_blocked(challenge):
    _, browser, _, choose, _, run = challenge
    result, _ = run("BLOCKED", goal="Complete login")
    assert result["stop_reason"] == "verification_required"
    assert "Only if the user's goal explicitly asks" in choose.call_args.args[1]
    browser.act.assert_not_called()


def test_denied_code_request_keeps_pending_challenge_and_never_clicks(challenge):
    _, browser, chrome, choose, _, run = challenge
    pending = copy.deepcopy(chrome.session_data["pending_verifications"])
    result, _ = run(allow=False)
    assert result["status"] == "denied"
    assert result["step_count"] == 0
    assert choose.call_count == 1
    assert chrome.session_data["pending_verifications"] == pending
    browser.act.assert_not_called()


@pytest.mark.parametrize("after_click", [False, True])
def test_stale_code_request_never_replays_and_returns_same_challenge(challenge, after_click):
    page, browser, chrome, choose, _, run = challenge
    if after_click:
        browser.observe.side_effect = [page, StalePage("Changed after input"), copy.deepcopy(page)]
    else:
        browser.act.side_effect = StalePage("Target changed during approval")
    result, _ = run()
    assert result["stop_reason"] == "verification_required"
    assert result["step_count"] == int(after_click)
    assert choose.call_count == browser.act.call_count == 1
    assert result["verification"]["url"] == page["url"]
    assert browser.target in chrome.session_data["pending_verifications"]


def test_navigation_during_decision_rejects_the_click_and_clears_old_challenge(challenge):
    page, browser, chrome, choose, _, run = challenge

    def choose_after_navigation(*_args, **_kwargs):
        page["url"] = "https://account.example.test/other"
        return choose.return_value

    choose.side_effect = choose_after_navigation
    result, _ = run()
    assert result["stop_reason"] == "verification_not_requested"
    assert chrome.session_data["pending_verifications"] == {}
    browser.act.assert_not_called()
