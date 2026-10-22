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


def test_delayed_password_login_masks_values_and_preserves_freshness(monkeypatch):
    from jev_ultrafast.browser import Browser, StalePage
    from jev_ultrafast.worker_browser import OwnedChrome

    secret = 'fixture-password-123'
    class Login(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<!doctype html><title>Login fixture</title><body><script>
            setTimeout(()=>{document.body.innerHTML=`<label>Username<input id="user"></label>
            <button id="next">Next</button>`;
            document.querySelector('#next').onclick=()=>setTimeout(()=>{
              document.body.innerHTML=`<label>Password<input id="pw" type="password"></label>
              <button id="login">Login</button><p></p>`;
              document.querySelector('#login').onclick=()=>{
                if(document.querySelector('#pw').value==='fixture-password-123')
                  document.querySelector('p').textContent='Authenticated fixture';
              };
            },350);},350);</script>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Login)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('TINK_BROWSER_HEADLESS', '1')
    try:
        with OwnedChrome() as chrome:
            browser = Browser(f'http://127.0.0.1:{server.server_port}', transport=chrome.call)
            try:
                page = browser.observe(screenshot=False)
                assert 'Username' in page['text']  # Initial empty SPA must settle.
                field = next(a for a in page['actions'] if a['kind'] == 'fill')
                browser.act(field, page, text='fixture-user')
                page = browser.observe(screenshot=False)
                next_button = next(a for a in page['actions'] if a['label'] == 'Next')
                browser.act(next_button, page)
                page = browser.observe(screenshot=False)
                deadline = time.monotonic() + 3
                while not any(a.get('sensitive') for a in page['actions']) and time.monotonic() < deadline:
                    page = browser.wait_for_change(page)
                field = next(a for a in page['actions'] if a['kind'] == 'fill' and a.get('sensitive'))
                browser.act(field, page, text=secret)
                page = browser.observe(screenshot=False)
                assert secret not in json.dumps(page)
                field = next(a for a in page['actions'] if a['kind'] == 'fill')
                assert field['value'] == '[filled]'
                button = next(a for a in page['actions'] if a['label'] == 'Login')
                # Another password with the same length must still invalidate a stale submit.
                browser.evaluate("document.querySelector('#pw').value='different-secret-123'")
                with pytest.raises(StalePage):
                    browser.act(button, page)
                page = browser.observe(screenshot=False)
                field = next(a for a in page['actions'] if a['kind'] == 'fill')
                browser.act(field, page, text=secret)
                page = browser.observe(screenshot=False)
                button = next(a for a in page['actions'] if a['label'] == 'Login')
                browser.act(button, page)
                assert 'Authenticated fixture' in browser.observe(screenshot=False)['text']
                browser.evaluate("document.body.innerHTML='<h1>SMS/Email Authentication</h1>' +"
                                 "'<p>Enter the code sent to your phone</p><input placeholder=\"Type Here\">'")
                assert browser.observe(screenshot=False)['authenticationRequired'] is True
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_saved_profile_keeps_cookie_and_excludes_concurrent_owner(monkeypatch, tmp_path, fixture_url):
    from jev_ultrafast.browser import Browser
    from jev_ultrafast.worker_browser import OwnedChrome
    monkeypatch.setenv('TINK_BROWSER_HEADLESS', '1')
    monkeypatch.setenv('TINK_BROWSER_PROFILE_DIR', str(tmp_path / 'saved'))
    with OwnedChrome() as chrome:
        browser = Browser(fixture_url, transport=chrome.call)
        browser.evaluate("document.cookie='fixture_login=yes; max-age=3600; path=/'")
        with pytest.raises(RuntimeError, match='profile_busy'):
            with OwnedChrome():
                pass
        browser.close()
    with OwnedChrome() as chrome:
        browser = Browser(fixture_url, transport=chrome.call)
        assert 'fixture_login=yes' in browser.evaluate('document.cookie')
        browser.close()


@pytest.fixture(autouse=True)
def isolated_session_environment(monkeypatch):
    monkeypatch.delenv('TINK_BROWSER_PROFILE_DIR', raising=False)
    monkeypatch.delenv('TINK_BROWSER_SESSION_DIR', raising=False)


@pytest.fixture
def live_session(monkeypatch, tmp_path):
    from jev_ultrafast.worker_browser import OwnedChrome
    monkeypatch.setenv('TINK_BROWSER_HEADLESS', '1')
    monkeypatch.setenv('TINK_BROWSER_SESSION_DIR', str(tmp_path / 'session'))
    monkeypatch.setenv('TINK_BROWSER_PROFILE_DIR', str(tmp_path / 'profile'))
    metadata = tmp_path / 'session' / 'session.json'
    yield metadata
    if metadata.exists():
        with OwnedChrome(allow_launch=False) as chrome:
            chrome.shutdown()


def live_decision(choice):
    return {'choice': choice, 'operation': choice, 'target': None, 'confidence': 1,
            'probabilities': {choice: 1}, 'latency_ms': 0, 'model': 'offline-fixture', 'usage': {}}


def run_live_task(monkeypatch, url=None, choice='DONE', allow=True):
    monkeypatch.setattr(agent, 'choose', lambda *_a, **_k: live_decision(choice))
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.messages.put({'type': 'action_response', 'id': '1', 'allow': allow})
    return run_task(validate_request({'type': 'start', 'protocol_version': 1, 'goal': 'Read the fixture',
                                      'start_url': url}), protocol)


def live_evaluate(expression):
    from jev_ultrafast.browser import Browser
    from jev_ultrafast.worker_browser import OwnedChrome
    with OwnedChrome(allow_launch=False) as chrome:
        browser = Browser(None, transport=chrome.call, target=chrome.session_data['target_id'], owns_target=False)
        try:
            return browser.evaluate(expression)
        finally:
            browser.close()


def test_live_session_retains_exact_tab_and_memory_after_blocked(monkeypatch, live_session, fixture_url):
    first = run_live_task(monkeypatch, fixture_url, choice='BLOCKED')
    assert first['status'] == 'blocked', first
    assert first['persistent_session'] and not first['session_reused']
    metadata = json.loads(live_session.read_text())
    live_evaluate("sessionStorage.setItem('login','yes'); window.fixtureLive=42; "
                  "document.querySelector('p').textContent='Manual verification complete'")
    second = run_live_task(monkeypatch)
    assert second['status'] == 'completed', second
    assert second['session_reused'] and second['browser_session'] == first['browser_session']
    assert 'Manual verification complete' in second['evidence'][-1]['text']
    assert json.loads(live_session.read_text()) == metadata
    assert live_evaluate("[sessionStorage.getItem('login'),window.fixtureLive]") == ['yes', 42]
    # A second process cannot close a session while its action lease is held.
    from jev_ultrafast.worker_browser import OwnedChrome
    with OwnedChrome(allow_launch=False):
        closed = subprocess.run([sys.executable, '-m', 'jev_ultrafast.worker', '--close-browser'],
                                capture_output=True, text=True, timeout=10)
        assert json.loads(closed.stdout)['stop_reason'] == 'browser_profile_busy'


def test_missing_closed_and_recreated_target_are_reported(monkeypatch, live_session, fixture_url):
    from jev_ultrafast.worker_browser import OwnedChrome
    assert run_live_task(monkeypatch)['stop_reason'] == 'browser_session_missing'
    assert not live_session.exists()
    first = run_live_task(monkeypatch, fixture_url)
    assert first['status'] == 'completed', first
    with OwnedChrome(allow_launch=False) as chrome:
        old_target = chrome.session_data['target_id']
        chrome.call('Target.closeTarget', targetId=old_target)
    assert run_live_task(monkeypatch)['stop_reason'] == 'browser_target_closed'
    recreated = run_live_task(monkeypatch, fixture_url)
    assert recreated['status'] == 'completed' and not recreated['session_reused']
    assert json.loads(live_session.read_text())['target_id'] != old_target
    closed = subprocess.run([sys.executable, '-m', 'jev_ultrafast.worker', '--close-browser'],
                            capture_output=True, text=True, timeout=15)
    assert json.loads(closed.stdout)['stop_reason'] == 'browser_closed'
    assert not live_session.exists()
    assert run_live_task(monkeypatch)['stop_reason'] == 'browser_session_missing'
    # Closing an absent session is idempotent and does not launch Chrome.
    closed = subprocess.run([sys.executable, '-m', 'jev_ultrafast.worker', '--close-browser'],
                            capture_output=True, text=True, timeout=10)
    assert json.loads(closed.stdout)['status'] == 'completed'
    assert not live_session.exists()


@pytest.mark.parametrize('headless', [True, pytest.param(False, marks=pytest.mark.skipif(
    sys.platform != 'darwin', reason='Headed last-window context unloading is a macOS regression',
))])
def test_all_windows_closed_can_resume_reopen_and_close(monkeypatch, live_session, fixture_url, headless):
    from jev_ultrafast.worker_browser import OwnedChrome

    monkeypatch.setenv('TINK_BROWSER_HEADLESS', '1' if headless else '0')
    first = run_live_task(monkeypatch, fixture_url)
    assert first['status'] == 'completed', first
    original = json.loads(live_session.read_text())
    live_evaluate("document.cookie='fixture_login=yes; max-age=3600; path=/'")

    def close_all_windows():
        with OwnedChrome(allow_launch=False) as chrome:
            for target in chrome.call('Target.getTargets')['targetInfos']:
                if target['type'] == 'page':
                    chrome.call('Target.closeTarget', targetId=target['targetId'])
            deadline = time.monotonic() + 10
            while True:
                targets = chrome.call('Target.getTargets')['targetInfos']
                if not targets or (headless and not any(t['type'] == 'page' for t in targets)):
                    break
                assert time.monotonic() < deadline, targets
                time.sleep(0.05)
            assert chrome._owned_process_alive()

    close_all_windows()
    # No URL may neither open a new window nor silently navigate away from a closed tab.
    resumed = run_live_task(monkeypatch)
    assert resumed['stop_reason'] == 'browser_target_closed', resumed
    assert resumed['usage']['decision_calls'] == 0
    assert json.loads(live_session.read_text()) == original
    with OwnedChrome(allow_launch=False) as chrome:
        assert not any(t['type'] == 'page' for t in chrome.call('Target.getTargets')['targetInfos'])

    reopened = run_live_task(monkeypatch, fixture_url)
    assert reopened['status'] == 'completed', reopened
    assert reopened['browser_session'] == first['browser_session'] and not reopened['session_reused']
    current = json.loads(live_session.read_text())
    assert current['pid'] == original['pid'] and current['target_id'] != original['target_id']
    assert 'fixture_login=yes' in live_evaluate('document.cookie')
    assert 'Ready' in reopened['evidence'][-1]['text']

    close_all_windows()
    closed = subprocess.run([sys.executable, '-m', 'jev_ultrafast.worker', '--close-browser'],
                            capture_output=True, text=True, timeout=15)
    assert json.loads(closed.stdout)['stop_reason'] == 'browser_closed', closed.stdout
    assert not live_session.exists()


@pytest.fixture
def popup_url():
    class Popup(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/tickets'):
                body = b'<title>Tickets</title><h1>Ticket options</h1><p>General admission available</p>'
            elif self.path.startswith('/source'):
                delay = 300 if 'delayed' in self.path else 0
                body = (f'''<!doctype html><title>Festival</title><h1>Festival</h1>
                    <button onclick="window.clicks=(window.clicks||0)+1;
                      setTimeout(()=>window.open('/tickets','_blank'),{delay})">Buy Passes</button>''').encode()
            else:
                body = b'<title>Unrelated</title><h1>Unrelated tab</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Popup)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('delay', ['', '?delayed'])
def test_click_follows_owned_popup_and_next_task_resumes_it(monkeypatch, live_session, popup_url, delay):
    from jev_ultrafast.browser import Browser
    from jev_ultrafast.worker_browser import OwnedChrome

    first = run_live_task(monkeypatch, popup_url + '/source' + delay)
    assert first['status'] == 'completed', first
    original = json.loads(live_session.read_text())
    with OwnedChrome(allow_launch=False) as chrome:
        browser = Browser(None, transport=chrome.call, target=original['target_id'], owns_target=False)
        try:
            browser.evaluate("document.cookie='fixture_login=yes; path=/'")
            browser.call('Runtime.evaluate', expression="window.open('/old-child','_blank')", userGesture=True)
            chrome.call('Target.createTarget', url=popup_url + '/unrelated', background=True)
            existing = {t['targetId'] for t in chrome.call('Target.getTargets')['targetInfos']}
        finally:
            browser.close()

    def choose(page, *_args, **_kwargs):
        if 'Ticket options' in page['text']:
            return live_decision('DONE')
        target = next(a for a in page['actions'] if a['kind'] == 'click' and a['label'] == 'Buy Passes')
        return live_decision(target['id'])

    monkeypatch.setattr(agent, 'choose', choose)
    protocol = Protocol(io.StringIO(), io.StringIO())
    protocol.messages.put({'type': 'action_response', 'id': '1', 'allow': True})
    opened = run_task(validate_request({'type': 'start', 'protocol_version': 1,
                                       'goal': 'Open ticket options', 'start_url': None}), protocol)
    assert opened['status'] == 'completed' and opened['step_count'] == 1, opened
    assert 'Ticket options' in opened['evidence'][-1]['text']
    current = json.loads(live_session.read_text())
    assert current['pid'] == original['pid'] and current['browser_session'] == original['browser_session']
    assert current['target_id'] not in existing
    assert 'fixture_login=yes' in live_evaluate('document.cookie')
    live_evaluate("window.popupMemory=42;sessionStorage.setItem('ticket','retained')")
    with OwnedChrome(allow_launch=False) as chrome:
        target = next(t for t in chrome.call('Target.getTargets')['targetInfos']
                      if t['targetId'] == current['target_id'])
        assert target['openerId'] == original['target_id']
        browser = Browser(None, transport=chrome.call, target=original['target_id'], owns_target=False)
        try:
            assert browser.evaluate('window.clicks') == 1
        finally:
            browser.close()
    resumed = run_live_task(monkeypatch)
    assert resumed['status'] == 'completed' and resumed['session_reused'], resumed
    assert 'Ticket options' in resumed['evidence'][-1]['text']
    assert live_evaluate("[window.popupMemory,sessionStorage.getItem('ticket')]") == [42, 'retained']


@pytest.mark.parametrize('stop', ['deny', 'cancel'])
def test_denied_or_cancelled_popup_click_never_opens_child(monkeypatch, live_session, popup_url, stop):
    from jev_ultrafast.worker import Stopped
    from jev_ultrafast.worker_browser import OwnedChrome

    assert run_live_task(monkeypatch, popup_url + '/source')['status'] == 'completed'
    original = json.loads(live_session.read_text())

    def choose(page, *_args, **_kwargs):
        return live_decision(next(a['id'] for a in page['actions'] if a['label'] == 'Buy Passes'))

    monkeypatch.setattr(agent, 'choose', choose)
    protocol = Protocol(io.StringIO(), io.StringIO())
    if stop == 'deny':
        protocol.messages.put({'type': 'action_response', 'id': '1', 'allow': False})
    else:
        def cancelled(**_kwargs):
            protocol.stop = Stopped('cancelled', 'cancelled')
            return {'type': 'action_response', 'id': '1', 'allow': True}
        protocol.messages.get = cancelled
    result = run_task(validate_request({'type': 'start', 'protocol_version': 1,
                                       'goal': 'Open ticket options', 'start_url': None}), protocol)
    assert result['status'] == ('denied' if stop == 'deny' else 'cancelled') and result['step_count'] == 0
    assert json.loads(live_session.read_text()) == original
    with OwnedChrome(allow_launch=False) as chrome:
        assert not any(t.get('openerId') == original['target_id']
                       for t in chrome.call('Target.getTargets')['targetInfos'])
    assert live_evaluate('window.clicks || 0') == 0


@pytest.mark.parametrize('phase,stop', [('model', 'cancel'), ('approval', 'cancel'),
                                        ('model', 'kill'), ('approval', 'kill'), ('approval', 'deny'),
                                        ('model', 'timeout')])
def test_task_stop_retains_live_browser_without_late_mutation(monkeypatch, live_session, fixture_url, phase, stop):
    opened = run_live_task(monkeypatch, fixture_url)
    assert opened['status'] == 'completed', opened
    original = json.loads(live_session.read_text())
    live_evaluate("window.fixtureLive=42;sessionStorage.setItem('login','yes')")
    choose = ("lambda *a,**k: time.sleep(60)" if phase == 'model' else
              "lambda page,*a,**k: dict(choice=next(a['id'] for a in page['actions'] "
              "if a['kind']=='click' and a['label']=='Save'),operation='CLICK',target=None,"
              "confidence=1,probabilities={},latency_ms=0,model='fixture',usage={})")
    code = ("import time; from jev_ultrafast import agent; agent.choose=" + choose +
            "; from jev_ultrafast.worker import main; main()")
    environment = {key: value for key, value in os.environ.items() if not any(
        word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')
    )}
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True, env=environment)
    try:
        child.stdin.write(json.dumps({'type': 'start', 'protocol_version': 1, 'goal': 'Read fixture',
                                      'timeout_seconds': 10}) + '\n')
        child.stdin.flush()
        assert json.loads(child.stdout.readline())['type'] == 'started'
        assert json.loads(child.stdout.readline())['type'] == 'observation'
        proposal = None
        if phase == 'approval':
            proposal = json.loads(child.stdout.readline())
            assert proposal['type'] == 'action_proposal'
        if stop == 'kill':
            os.killpg(child.pid, signal.SIGKILL)
        elif stop == 'deny':
            child.stdin.write(json.dumps({'type': 'action_response', 'id': proposal['id'], 'allow': False}) + '\n')
            child.stdin.flush()
        elif stop == 'cancel':
            child.stdin.write('{"type":"cancel"}\n')
            child.stdin.flush()
        child.wait(timeout=15)
        if stop != 'kill':
            result = json.loads(child.stdout.read())
            expected = {'deny': 'denied', 'cancel': 'cancelled', 'timeout': 'timeout'}[stop]
            assert result['status'] == expected, result
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
    assert json.loads(live_session.read_text()) == original
    values = live_evaluate("[sessionStorage.getItem('login'),window.fixtureLive,"
                           "document.querySelector('p').textContent]")
    assert values == ['yes', 42, 'Ready']
    resumed = run_live_task(monkeypatch)
    assert resumed['status'] == 'completed' and resumed['session_reused']
