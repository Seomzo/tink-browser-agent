"""Opt-in real Chrome tests: TINK_TEST_OWNED_CHROME=1 pytest tests/test_worker_chrome.py.

All model decisions are offline stubs. The tests own a temporary Chrome profile and
a localhost fixture; they never attach to an existing browser or make paid calls.
"""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent
from jev_ultrafast.worker import Protocol, run_task, validate_request

pytestmark = pytest.mark.skipif(
    os.environ.get("TINK_TEST_OWNED_CHROME") != "1" or sys.platform == "win32",
    reason="Opt in to disposable Chrome tests with TINK_TEST_OWNED_CHROME=1 on macOS/Linux",
)


@pytest.fixture
def fixture_url():
    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'''<!doctype html><title>Worker fixture</title>
                <label>Book <input aria-label="Book"></label>
                <button onclick="document.querySelector('p').textContent=
                    'Saved '+document.querySelector('input').value">Save</button><p>Ready</p>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def test_real_fixture_fill_click_and_visible_outcome(monkeypatch, fixture_url):
    protocol = Protocol(io.StringIO(), io.StringIO())
    for index in range(1, 4):
        protocol.messages.put({"type": "action_response", "id": str(index), "allow": True})

    def choose(page, *_args, **_kwargs):
        if "Saved book" in page["text"]:
            selected, operation = "DONE", "DONE"
        else:
            field = next(item for item in page["actions"] if item["kind"] == "fill")
            target = field if not field.get("value") else next(
                item for item in page["actions"] if item["kind"] == "click" and item["label"] == "Save"
            )
            selected = target["id"]
            operation = "TYPE_TEXT" if target["kind"] == "fill" else "CLICK"
        return {"choice": selected, "operation": operation, "target": None, "confidence": 1,
                "probabilities": {selected: 1}, "latency_ms": 0, "model": "offline-fixture", "usage": {}}

    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "field_text", Mock(return_value=("book", {
        "model": "offline-fixture", "latency_ms": 0, "usage": {},
    })))
    result = run_task(validate_request({"type": "start", "protocol_version": 1, "goal": "Save book",
                                        "start_url": fixture_url}), protocol)
    assert result["status"] == "completed", result
    assert result["step_count"] == 2
    assert [step["kind"] for step in result["steps"]] == ["fill", "click"]
    assert "Saved book" in result["evidence"][-1]["text"]
    assert result["outcome_verified"] is False  # Verification belongs to the test/host, not the policy.


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_inflight_model_interrupt_closes_owned_chrome(mode, fixture_url):
    code = (
        "from jev_ultrafast import agent; import time; "
        "agent.choose=lambda *a,**k: time.sleep(60); "
        "from jev_ultrafast.worker import main; main()"
    )
    environment = {key: value for key, value in os.environ.items() if not any(
        word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
    )}
    environment["TINK_BROWSER_HEADLESS"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=environment,
    )
    try:
        child.stdin.write(json.dumps({"type": "start", "protocol_version": 1, "goal": "Read page",
                                      "start_url": fixture_url, "timeout_seconds": 10}) + "\n")
        child.stdin.flush()
        proposal = json.loads(child.stdout.readline())
        assert proposal["type"] == "action_proposal"
        child.stdin.write(json.dumps({"type": "action_response", "id": proposal["id"], "allow": True}) + "\n")
        child.stdin.flush()
        assert json.loads(child.stdout.readline())["type"] == "started"
        assert json.loads(child.stdout.readline())["type"] == "observation"
        if mode == "cancel":
            child.stdin.write('{"type":"cancel"}\n')
            child.stdin.flush()
        child.wait(timeout=15)
        result = json.loads(child.stdout.read())
        assert result["status"] == ("cancelled" if mode == "cancel" else "timeout")
        assert len(result["evidence"]) == 1
        assert result["usage"]["cost_complete"] is False
        time.sleep(0.2)
        listing = subprocess.check_output(["ps", "-axo", "pgid=,comm="], text=True)
        assert not [line for line in listing.splitlines() if line.split(maxsplit=1)[0] == str(child.pid)]
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def test_parent_owned_temp_directory_removes_profile_after_hard_kill(tmp_path, fixture_url):
    code = (
        "from jev_ultrafast import agent; import time; "
        "agent.choose=lambda *a,**k: time.sleep(60); "
        "from jev_ultrafast.worker import main; main()"
    )
    environment = {key: value for key, value in os.environ.items() if not any(
        word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
    )}
    environment["TINK_BROWSER_HEADLESS"] = "1"
    # Mirrors the host: the child can be force-killed, but the parent owns cleanup.
    with tempfile.TemporaryDirectory(dir=tmp_path, prefix="host-browser-task-") as workdir:
        environment["TMPDIR"] = workdir
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=environment, cwd=workdir,
        )
        try:
            child.stdin.write(json.dumps({"type": "start", "protocol_version": 1, "goal": "Read page",
                                          "start_url": fixture_url, "timeout_seconds": 30}) + "\n")
            child.stdin.flush()
            proposal = json.loads(child.stdout.readline())
            assert proposal["type"] == "action_proposal"
            child.stdin.write(json.dumps({"type": "action_response", "id": proposal["id"], "allow": True}) + "\n")
            child.stdin.flush()
            assert json.loads(child.stdout.readline())["type"] == "started"
            assert json.loads(child.stdout.readline())["type"] == "observation"
            profiles = list(Path(workdir).glob("tink-browser-*"))
            assert len(profiles) == 1
            assert (profiles[0] / "Default").is_dir()
        finally:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        assert profiles[0].exists()  # The killed worker could not run its own cleanup.
    assert not Path(workdir).exists()
    assert not profiles[0].exists()
