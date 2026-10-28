"""Observed-action round trips through real, isolated Chrome without model calls."""

import io
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import Browser
from jev_ultrafast.worker import Protocol, Stopped, remember_actions, run_task, validate_request
from jev_ultrafast.worker_browser import OwnedChrome

pytestmark = pytest.mark.skipif(
    os.environ.get("TINK_TEST_OWNED_CHROME") != "1" or sys.platform == "win32",
    reason="Opt in to isolated Chrome with TINK_TEST_OWNED_CHROME=1 on macOS/Linux",
)


@pytest.fixture
def session(monkeypatch, tmp_path):
    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<!doctype html><title>Observed action fixture</title>
                <style>button,input,select{display:block;width:200px;height:40px;margin:10px}</style>
                <form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">
                  <input id="query" aria-label="Query" value="observed query"></form>
                <button id="count" onclick="window.clicks=(window.clicks||0)+1">Count</button>
                <select id="mode" aria-label="Mode"><option>All</option><option>Closed</option></select>
                <p>Local fixture</p>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("TINK_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.delenv("TINK_BROWSER_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=AssertionError("No model calls")))
    monkeypatch.setattr(loop, "field_text", Mock(side_effect=AssertionError("No text helper")))
    url = f"http://127.0.0.1:{server.server_port}/"
    with OwnedChrome() as chrome:
        browser = Browser(url, transport=chrome.call, owns_target=False)
        target = browser.target
        chrome.retain_target(target)
        browser.close()
    try:
        yield target, url
    finally:
        with OwnedChrome(allow_launch=False) as chrome:
            chrome.shutdown()
        server.shutdown()
        server.server_close()


@contextmanager
def attached(target):
    with OwnedChrome(allow_launch=False) as chrome:
        browser = Browser(None, transport=chrome.call, target=target, owns_target=False)
        try:
            yield chrome, browser
        finally:
            browser.close()


def reference(target, kind="click"):
    with attached(target) as (chrome, browser):
        if kind == "key":
            browser.evaluate("document.querySelector('#query').focus()")
        elif kind == "back":
            browser.evaluate("history.pushState({},'', '#detail')")
        page = browser.observe(screenshot=False)
        action = next(action for action in page["actions"] if action["kind"] == kind
                      and (kind != "click" or action["label"] == "Count"))
        return remember_actions(chrome, target, page)[action["id"]]


def perform(target, token, protocol=None):
    if protocol is None:
        protocol = Protocol(io.StringIO(), io.StringIO())
        protocol.messages.put({"type": "action_response", "id": "1", "allow": True})
    request = validate_request({"type": "start", "protocol_version": 1, "action": "perform",
                                "tab_id": target, "action_ref": token})
    return run_task(request, protocol)


@pytest.mark.parametrize("kind,expression,expected", [
    ("click", "window.clicks", 1), ("select", "document.querySelector('#mode').value", "Closed"),
    ("key", "window.submits", 1), ("back", "location.hash", ""),
])
def test_reconnect_performs_exact_observed_nontext_action_once(session, kind, expression, expected):
    target, _ = session
    token = reference(target, kind)
    result = perform(target, token)
    assert (result["status"], result["stop_reason"], result["step_count"]) == ("completed", "action_performed", 1)
    assert result["usage"]["decision_calls"] == result["usage"]["text_calls"] == 0
    assert result["usage"]["cost_complete"] is True
    with attached(target) as (_, browser):
        assert browser.evaluate(expression) == expected
    replay = perform(target, token)
    assert (replay["stop_reason"], replay["step_count"]) == ("action_ref_unavailable", 0)
    with attached(target) as (_, browser):
        assert browser.evaluate(expression) == expected
    loop.choose.assert_not_called()
    loop.field_text.assert_not_called()


@pytest.mark.parametrize("change", ["reload", "field", "replace", "label", "other_tab"])
def test_reconnected_stale_or_foreign_page_cannot_reuse_observed_reference(session, change):
    target, url = session
    token = reference(target)
    with attached(target) as (chrome, browser):
        if change == "reload":
            origin = browser.evaluate("performance.timeOrigin")
            browser.call("Page.reload")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if browser.evaluate("performance.timeOrigin") != origin:
                    break
                time.sleep(0.02)
            assert browser.evaluate("performance.timeOrigin") != origin
        elif change == "other_tab":
            target = chrome.call("Target.createTarget", url=url, background=True)["targetId"]
        elif change == "field":
            browser.evaluate("document.querySelector('#query').value='different query'")
        elif change == "replace":
            browser.evaluate("document.querySelector('#count').outerHTML=document.querySelector('#count').outerHTML")
        else:
            browser.evaluate("document.querySelector('#count').textContent='Delete record'")
    result = perform(target, token)
    assert (result["stop_reason"], result["step_count"], result["approval_count"]) == ("action_ref_unavailable", 0, 0)
    assert result["available_actions"]
    with attached(target) as (_, browser):
        assert browser.evaluate("window.clicks || 0") == 0
    loop.choose.assert_not_called()


@pytest.mark.parametrize("mode", ["deny", "cancel", "change_during_approval"])
def test_real_approval_boundary_consumes_ref_without_mutation(session, mode):
    target, _ = session
    token = reference(target)

    class Supervision(Protocol):
        def emit(self, event_type, **body):
            super().emit(event_type, **body)
            if event_type != "action_proposal":
                return
            if mode == "cancel":
                self.stop = Stopped("cancelled", "cancelled")
            elif mode == "change_during_approval":
                # Use this task's already-attached session; no second lease is taken.
                observed_browser[0].evaluate("document.querySelector('#count').textContent='Changed purpose'")
            self.messages.put({"type": "action_response", "id": body["id"], "allow": mode != "deny"})

    observed_browser = []

    def factory(*args, **kwargs):
        browser = Browser(*args, **kwargs)
        observed_browser.append(browser)
        return browser

    protocol = Supervision(io.StringIO(), io.StringIO())
    req = validate_request({"type": "start", "protocol_version": 1, "action": "perform",
                            "tab_id": target, "action_ref": token})
    result = run_task(req, protocol, browser_factory=factory)
    assert result["stop_reason"] == {"deny": "approval_denied", "cancel": "cancelled",
                                      "change_during_approval": "action_ref_unavailable"}[mode]
    assert result["step_count"] == 0 and result["usage"]["cost_complete"] is True
    with attached(target) as (chrome, browser):
        assert browser.evaluate("window.clicks || 0") == 0
        assert token not in json.dumps(chrome.session_data.get("action_refs"))
    loop.choose.assert_not_called()
