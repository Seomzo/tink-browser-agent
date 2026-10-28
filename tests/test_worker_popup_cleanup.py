"""Non-model terminal paths cannot abandon an already-approved click's pending popup."""

import io
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.worker import Protocol, Stopped, run_task, validate_request


@pytest.mark.parametrize("stop", ["step_limit", "decision_limit", "approval_denied", "cancelled", "deadline_exceeded"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_worker_stop_settles_pending_popup_without_replay_or_losing_reason(monkeypatch, stop, cleanup_fails):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", "/isolated/session")
    monkeypatch.delenv("TINK_BROWSER_DOWNLOAD_DIR", raising=False)
    action = {"id": "e1", "kind": "click", "node": 1, "label": "Open details", "role": "button"}
    source = {"url": "https://example.test/source", "title": "Source", "text": "Source", "fingerprint": "source",
              "page_key": [123, "source"], "guards": {"1": [1, "Open details"]}, "actions": [action]}
    child = {**deepcopy(source), "url": "https://example.test/child", "title": "Child", "text": "Child evidence",
             "fingerprint": "child", "page_key": [456, "child"]}
    browser = Mock(target="source", pending_tabs=None, observe=Mock(return_value=source))
    chrome = Mock(session_data={"browser_session": "owned-session", "target_id": "source"})
    chrome.__enter__ = Mock(return_value=chrome)
    chrome.__exit__ = Mock(return_value=False)
    chrome.call.side_effect = lambda method, **_kwargs: {"targetInfos": [
        {"targetId": "source", "type": "page"}, {"targetId": "child", "type": "page"},
    ]}
    protocol = Protocol(io.StringIO(), io.StringIO())

    def approve(*_args, **_kwargs):
        if browser.pending_tabs and stop == "approval_denied":
            raise Stopped("denied", stop)

    protocol.approve = approve
    browser.act.side_effect = lambda *_args, **_kwargs: setattr(browser, "pending_tabs", ("source", {"source"}, 1.0))

    def fresh(_page, *, settle_tabs=False):
        if settle_tabs and browser.pending_tabs:
            if cleanup_fails:
                raise RuntimeError("Discovery interrupted")
            browser.pending_tabs = None
            browser.target = "child"
            browser.observe.return_value = child
            chrome.retain_target("child")
            return False
        return True

    browser.fresh.side_effect = fresh
    decision_calls = 0

    def choose(*_args, **_kwargs):
        nonlocal decision_calls
        decision_calls += 1
        if decision_calls == 2 and stop in {"cancelled", "deadline_exceeded"}:
            raise Stopped("cancelled" if stop == "cancelled" else "timeout", stop)
        return {"choice": "e1", "operation": "CLICK", "target": "1", "probabilities": {"e1": 1},
                "confidence": 1, "latency_ms": 0, "usage": {"cost": 0}}

    monkeypatch.setattr(loop, "choose", choose)
    agent_factory = loop.Agent
    if stop == "decision_limit":
        class LimitedAgent(loop.Agent):
            def command(self, name, body=None):
                result = super().command(name, body)
                if name == "act":
                    self.state["decisions"] *= 2
                return result
        agent_factory = LimitedAgent
    req = validate_request({"type": "start", "protocol_version": 1, "goal": "Open details",
                            "max_steps": 1 if stop in {"step_limit", "decision_limit"} else 20})
    result = run_task(req, protocol, chrome_factory=Mock(return_value=chrome),
                      browser_factory=Mock(return_value=browser), agent_factory=agent_factory)
    assert result["stop_reason"] == stop
    assert result["step_count"] == 1 and browser.act.call_count == 1
    assert any(call.kwargs.get("settle_tabs") for call in browser.fresh.call_args_list)
    if cleanup_fails:
        assert result["current_tab_id"] == "source"
        assert "action_refs" not in chrome.session_data
        assert all("action_ref" not in action for action in result.get("available_actions", []))
    else:
        assert result["current_tab_id"] == "child" and result["evidence"][-1]["text"] == "Child evidence"
        chrome.retain_target.assert_called_once_with("child")
    browser.close.assert_called_once()
