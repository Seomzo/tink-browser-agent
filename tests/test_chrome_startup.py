"""Chrome endpoint publication races, using real files and offline process/clock doubles."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import worker_browser
from jev_ultrafast.worker_browser import OwnedChrome

ENDPOINT_FILE = "12345\n/devtools/browser/owned\n"
ENDPOINT = "ws://127.0.0.1:12345/devtools/browser/owned"


@pytest.fixture(params=[False, True], ids=["disposable", "persistent"])
def startup(request, monkeypatch, tmp_path):
    persistent = request.param
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("TINK_BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.delenv("TINK_BROWSER_SESSION_DIR", raising=False)
    if persistent:
        monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.setattr(worker_browser, "chrome_binary", lambda _=None: "/owned/chrome")
    process = Mock(pid=123, poll=Mock(return_value=None))

    def wait(**_kwargs):
        process.poll.return_value = 0
        return 0

    process.wait.side_effect = wait
    port_file = profile / "DevToolsActivePort"
    state = SimpleNamespace(now=0.0, initial=None, on_sleep=lambda: None, command=[])

    def launch(command, **_kwargs):
        state.command = command
        if state.initial is not None:
            port_file.write_text(state.initial)
        return process

    def sleep(seconds):
        state.now += seconds
        state.on_sleep()

    launch = Mock(side_effect=launch)
    monkeypatch.setattr(worker_browser.subprocess, "Popen", launch)
    ownership = Mock(side_effect=lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout="S " + " ".join(state.command),
    ))
    monkeypatch.setattr(worker_browser.subprocess, "run", ownership)
    monkeypatch.setattr(worker_browser, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    socket = Mock()

    def reply(**_kwargs):
        sent = json.loads(socket.send.call_args.args[0])
        result = {"targetInfos": [{"type": "page"}]} if sent["method"] == "Target.getTargets" else {}
        return json.dumps({"id": sent["id"], "result": result})

    socket.recv.side_effect = reply
    connect = Mock(return_value=socket)
    monkeypatch.setattr(worker_browser, "connect", connect)
    return SimpleNamespace(persistent=persistent, process=process, port_file=port_file, state=state,
                           connect=connect, launch=launch, ownership=ownership)


def test_waits_through_empty_and_partial_publication(startup):
    stages = iter(["", "12345\n", "12345\n\n", ENDPOINT_FILE])
    startup.state.on_sleep = lambda: startup.port_file.write_text(next(stages))
    with OwnedChrome() as chrome:
        assert startup.state.now == pytest.approx(0.2)
        startup.connect.assert_called_once()
        assert startup.connect.call_args.args == (ENDPOINT,)
        if startup.persistent:
            assert startup.ownership.call_count > 0
            assert chrome.session_data["endpoint"] == ENDPOINT
    startup.launch.assert_called_once()


def test_complete_endpoint_does_not_add_a_startup_delay(startup):
    startup.state.initial = ENDPOINT_FILE
    with OwnedChrome():
        assert startup.state.now == 0
        startup.connect.assert_called_once()


@pytest.mark.parametrize("partial", [None, "", "12345\n", "12345\n\n"])
def test_unfinished_endpoint_expires_without_connecting(startup, partial):
    startup.state.initial = partial
    chrome = OwnedChrome()
    with pytest.raises(RuntimeError, match="^chrome_start_failed$"):
        chrome.__enter__()
    assert 15 <= startup.state.now < 15.05
    startup.connect.assert_not_called()
    startup.launch.assert_called_once()
    assert chrome.profile_lock is None and chrome.session_lock is None


def test_process_exit_ends_partial_endpoint_wait(startup):
    startup.state.initial = "12345\n"
    startup.state.on_sleep = lambda: setattr(startup.process.poll, "return_value", 7)
    with pytest.raises(RuntimeError, match="^chrome_start_failed$"):
        with OwnedChrome():
            pass
    assert startup.state.now == pytest.approx(0.05)
    startup.connect.assert_not_called()


def test_endpoint_completed_after_deadline_is_not_used(startup):
    def publish_too_late():
        startup.state.now = 15
        startup.port_file.write_text(ENDPOINT_FILE)

    startup.state.on_sleep = publish_too_late
    with pytest.raises(RuntimeError, match="^chrome_start_failed$"):
        with OwnedChrome():
            pass
    startup.connect.assert_not_called()


@pytest.mark.parametrize("startup", [True], indirect=True, ids=["persistent"])
def test_partial_publication_does_not_bypass_persistent_process_identity(startup):
    startup.state.initial = "12345\n"
    startup.ownership.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout="S /unrelated/chrome --remote-debugging-port=0",
    )
    with pytest.raises(RuntimeError, match="^browser_session_unavailable$"):
        with OwnedChrome():
            pass
    startup.connect.assert_not_called()
    assert startup.state.now == 0


@pytest.mark.parametrize("startup", [True], indirect=True, ids=["persistent"])
def test_completed_publication_cannot_replace_saved_endpoint(startup):
    startup.state.initial = ENDPOINT_FILE
    with OwnedChrome():
        pass
    startup.connect.reset_mock()
    startup.port_file.write_text("")
    startup.state.on_sleep = lambda: startup.port_file.write_text("2222\n/devtools/browser/different\n")
    with pytest.raises(RuntimeError, match="^browser_session_unavailable$"):
        with OwnedChrome(allow_launch=False):
            pass
    startup.connect.assert_not_called()
    startup.launch.assert_called_once()
