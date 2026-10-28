"""One-use observed actions bypass model decisions without bypassing supervision."""

import io
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage
from jev_ultrafast.worker import Protocol, Stopped, remember_actions, run_task, validate_request


def request(**overrides):
    return validate_request({"type": "start", "protocol_version": 1, "action": "perform",
                             "tab_id": "tab", "action_ref": "a" * 64, **overrides})


def page():
    return {"url": "https://example.test/", "title": "Observed fixture", "text": "Ready", "fingerprint": "page",
            "page_key": [123, "https://example.test/", ["PRIVATE INPUT VALUE"]],
            "guards": {"1": [1, "PRIVATE GUARD VALUE"], "2": [2, "Second target"]},
            "actions": [
                {"id": "e1", "kind": "click", "node": 1, "role": "button", "label": "Continue"},
                {"id": "e2", "kind": "fill", "node": 2, "role": "textbox", "label": "Name"},
                {"id": "wait", "kind": "wait", "label": "Wait"},
            ]}


def setup_perform(monkeypatch, observed=None, *, allow=True):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/isolated/test-session")
    monkeypatch.delenv("TINK_BROWSER_DOWNLOAD_DIR", raising=False)
    observed = observed or page()
    chrome = Mock(session_data={"browser_session": "owned-session", "target_id": "tab"})
    chrome.__enter__ = Mock(return_value=chrome)
    chrome.__exit__ = Mock(return_value=False)
    chrome.call.return_value = {"targetInfos": [{"targetId": "tab", "type": "page"}]}
    token = remember_actions(chrome, "tab", observed)["e1"]
    browser = Mock(target="tab", pending_tabs=None, observe=Mock(return_value=deepcopy(observed)),
                   fresh=Mock(return_value=True))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.messages.put({"type": "action_response", "id": "1", "allow": allow})
    monkeypatch.setattr(loop, "choose", Mock(side_effect=AssertionError("No model decision allowed")))
    monkeypatch.setattr(loop, "field_text", Mock(side_effect=AssertionError("No text generation allowed")))
    return token, chrome, browser, protocol


def perform(token, chrome, browser, protocol, **overrides):
    return run_task(request(action_ref=token, **overrides), protocol,
                    chrome_factory=Mock(return_value=chrome), browser_factory=Mock(return_value=browser))


@pytest.mark.parametrize("overrides", [
    {"tab_id": None}, {"action_ref": None}, {"action_ref": "a" * 63}, {"action_ref": "A" * 64},
    {"action_ref": 1}, {"start_url": "https://example.test/"}, {"new_tab": True},
    {"verification_response": "123456"}, {"action": "tabs"}, {"action": "run", "goal": "Click"},
])
def test_invalid_perform_contract_rejected_before_browser(overrides):
    with pytest.raises(ValueError, match="invalid_request"):
        request(**overrides)


@pytest.mark.parametrize("kind,extra", [
    ("click", {}), ("select", {"value": "private-selected-value"}), ("key", {"key": "Enter"}),
    ("back", {"entry_id": 1, "current_entry_id": 2}),
])
def test_perform_runs_one_approved_observed_action_without_any_model_call(monkeypatch, kind, extra):
    observed = page()
    observed["actions"][0].update(kind=kind, **extra)
    token, chrome, browser, protocol = setup_perform(monkeypatch, observed)
    original_approve = protocol.approve

    def approve(*args, **kwargs):
        assert "action_refs" not in chrome.session_data  # Persistently consumed before waiting or input.
        original_approve(*args, **kwargs)

    protocol.approve = approve
    result = perform(token, chrome, browser, protocol, max_steps=30)
    assert (result["status"], result["stop_reason"], result["step_count"]) == ("completed", "action_performed", 1)
    browser.act.assert_called_once_with(observed["actions"][0], observed, text=None)
    loop.choose.assert_not_called()
    loop.field_text.assert_not_called()
    assert result["usage"] == {"decision_calls": 0, "text_calls": 0, "input_tokens": 0,
                               "output_tokens": 0, "cost_usd": 0.0, "cost_complete": True}
    assert result["approval_count"] == 1 and result["outcome_verified"] is False
    fresh = result["available_actions"][0]["action_ref"]
    assert fresh != token and len(fresh) == 64
    assert all("action_ref" not in a for a in result["available_actions"][1:])
    assert "PRIVATE" not in json.dumps(chrome.session_data["action_refs"])
    assert "private-selected-value" not in json.dumps(chrome.session_data["action_refs"])


@pytest.mark.parametrize("mutation", ["session", "tab", "document", "field", "guard", "label", "unknown"])
def test_stale_or_foreign_reference_never_reinterprets_action_and_returns_fresh_controls(monkeypatch, mutation):
    token, chrome, browser, protocol = setup_perform(monkeypatch)
    journal, current = chrome.session_data["action_refs"], browser.observe.return_value
    if mutation == "session":
        journal["browser_session"] = "another-session"
    elif mutation == "tab":
        journal["tab_id"] = "another-tab"
    elif mutation == "document":
        current["page_key"][0] += 1
    elif mutation == "field":
        current["page_key"][2] = ["Changed field value"]
    elif mutation == "guard":
        current["guards"]["1"] = [1, "Changed purpose"]
    elif mutation == "label":
        current["actions"][0]["label"] = "Delete"
    else:
        token = "0" * 64
    result = perform(token, chrome, browser, protocol)
    assert (result["status"], result["stop_reason"], result["step_count"]) == ("blocked", "action_ref_unavailable", 0)
    browser.act.assert_not_called()
    loop.choose.assert_not_called()
    assert result["approval_count"] == 0 and result["available_actions"][0]["action_ref"] != token


def test_reference_replay_cannot_execute_same_unchanged_button_twice(monkeypatch):
    token, chrome, browser, protocol = setup_perform(monkeypatch)
    assert perform(token, chrome, browser, protocol)["step_count"] == 1
    result = perform(token, chrome, browser, Protocol(io.StringIO(), io.StringIO()))
    assert result["stop_reason"] == "action_ref_unavailable"
    assert browser.act.call_count == 1


@pytest.mark.parametrize("stop", ["deny", "cancel", "stale_after_approval"])
def test_perform_consumes_reference_when_execution_is_denied_cancelled_or_stale(monkeypatch, stop):
    token, chrome, browser, protocol = setup_perform(monkeypatch, allow=stop != "deny")
    if stop == "cancel":
        protocol.stop = Stopped("cancelled", "cancelled")
    elif stop == "stale_after_approval":
        browser.act.side_effect = StalePage("Changed during approval")
    result = perform(token, chrome, browser, protocol)
    assert result["step_count"] == 0
    assert result["stop_reason"] == {"deny": "approval_denied", "cancel": "cancelled",
                                      "stale_after_approval": "action_ref_unavailable"}[stop]
    assert token not in chrome.session_data.get("action_refs", {}).get("entries", {})
    assert result["usage"]["cost_complete"] is True
    loop.choose.assert_not_called()
    if stop != "stale_after_approval":
        browser.act.assert_not_called()


def test_pending_mfa_consumes_reference_without_approval_model_or_new_refs(monkeypatch):
    token, chrome, browser, protocol = setup_perform(monkeypatch)
    browser.observe.return_value["authenticationRequired"] = True
    result = perform(token, chrome, browser, protocol)
    assert result["stop_reason"] == "verification_required"
    assert "action_refs" not in chrome.session_data and "available_actions" not in result
    browser.act.assert_not_called()
    loop.choose.assert_not_called()
    assert result["approval_count"] == 0


def test_closed_tab_does_not_reissue_action_refs(monkeypatch):
    token, chrome, browser, protocol = setup_perform(monkeypatch)
    chrome.call.return_value = {"targetInfos": []}
    result = perform(token, chrome, browser, protocol)
    assert result["stop_reason"] == "browser_target_closed"
    assert "action_refs" not in chrome.session_data and "available_actions" not in result
    browser.act.assert_not_called()


def test_reference_journal_is_bounded_and_replaces_previous_observation():
    observed = page()
    observed["actions"] = [dict(observed["actions"][0], id=f"a{i}") for i in range(300)]
    chrome = Mock(session_data={"browser_session": "test-session", "action_refs": {"old": "discard"}})
    refs = remember_actions(chrome, "tab", observed)
    assert len(refs) == len(chrome.session_data["action_refs"]["entries"]) == 250
    assert len(set(refs.values())) == 250
    assert "old" not in json.dumps(chrome.session_data)
    assert "PRIVATE" not in json.dumps(chrome.session_data)


@pytest.mark.parametrize("allow,cancel,expected", [
    (True, False, "action_performed"), (False, False, "approval_denied"), (True, True, "cancelled"),
])
def test_optional_reference_persistence_failure_preserves_original_result(monkeypatch, allow, cancel, expected):
    token, chrome, browser, protocol = setup_perform(monkeypatch, allow=allow)
    # Consumption and pending-verification save succeed; only final reference storage fails.
    chrome._save_session.side_effect = [None, None, OSError("PRIVATE storage failure")]
    if cancel:
        protocol.stop = Stopped("cancelled", "cancelled")
    result = perform(token, chrome, browser, protocol)
    assert result["stop_reason"] == expected
    assert result["step_count"] == int(allow and not cancel)
    assert all("action_ref" not in action for action in result.get("available_actions", []))
    assert "action_refs" not in chrome.session_data and "PRIVATE" not in json.dumps(result)
