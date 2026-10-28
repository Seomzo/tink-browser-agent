"""Native sign-in ownership contracts without personal profiles or network calls."""

import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser_setup as setup
from jev_ultrafast.worker_browser import OwnedChrome


@pytest.fixture
def profile(tmp_path, monkeypatch):
    session, profile = tmp_path / "session", tmp_path / "profile"
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(session))
    monkeypatch.setenv("TINK_BROWSER_PROFILE_DIR", str(profile))
    return session, profile


def assert_locked(path):
    with path.open("a") as lock, pytest.raises(BlockingIOError):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_native_flags_no_credentials_and_leases_until_exit(profile, monkeypatch):
    session, directory = profile
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-chrome")
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    monkeypatch.setattr(setup, "chrome_binary", lambda _binary: "/fake/chrome")
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)
    process = Mock(pid=12345, poll=Mock(return_value=None))

    def wait():
        assert_locked(session / "session.lock")
        assert_locked(directory / ".tink-worker.lock")
        metadata = json.loads((session / "session.json").read_text())
        assert metadata["setup"] is True
        assert metadata["profile_dir"] == str(directory)
        process.poll.return_value = 0

    process.wait.side_effect = wait
    launch = Mock(return_value=process)
    monkeypatch.setattr(setup.subprocess, "Popen", launch)
    started = Mock()
    setup.open_signin(started=started)
    args = launch.call_args.args[0]
    assert f"--user-data-dir={directory}" in args
    assert args[-1] == "chrome://settings/people"
    assert not any(flag in " ".join(args) for flag in (
        "remote-debugging", "headless", "disable-sync", "disable-background-networking",
        "password-store", "enable-automation", "disable-extensions",
    ))
    assert "OPENROUTER_API_KEY" not in launch.call_args.kwargs["env"]
    started.assert_called_once_with({"status": "running"})
    process.terminate.assert_not_called()
    assert not (session / "session.json").exists()
    assert setup.setup_status() == {"status": "idle"}


def test_worker_winning_missing_session_gap_is_never_replaced(profile, monkeypatch):
    session, directory = profile

    def raced_enter(self):
        session.mkdir()
        directory.mkdir()
        (session / "session.json").write_text('{"new_worker":"owns this profile"}')
        raise RuntimeError("browser_session_missing")

    monkeypatch.setattr(OwnedChrome, "__enter__", raced_enter)
    launch = Mock()
    monkeypatch.setattr(setup.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="browser_profile_busy"):
        setup.open_signin()
    launch.assert_not_called()
    assert json.loads((session / "session.json").read_text())["new_worker"] == "owns this profile"


def test_idle_automated_browser_closes_without_releasing_lease(profile, monkeypatch):
    session, directory = profile
    session.mkdir()
    directory.mkdir()
    (session / "session.json").write_text(json.dumps({
        "pid": 123, "binary": "/fake/chrome", "profile_dir": str(directory), "browser_session": "a" * 32,
    }))
    monkeypatch.setattr(OwnedChrome, "_owned_process_alive", lambda _self: True)
    monkeypatch.setattr(OwnedChrome, "_connect_persistent", lambda _self: None)
    shutdown = []

    def close_owned(self):
        assert_locked(session / "session.lock")
        assert_locked(directory / ".tink-worker.lock")
        (session / "session.json").unlink()
        shutdown.append(True)

    monkeypatch.setattr(OwnedChrome, "shutdown", close_owned)
    monkeypatch.setattr(setup, "chrome_binary", lambda _binary: "/fake/chrome")
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)
    process = Mock(pid=456, poll=Mock(side_effect=[None, 0]))

    def launch(*args, **kwargs):
        assert shutdown == [True]
        assert_locked(session / "session.lock")
        assert_locked(directory / ".tink-worker.lock")
        return process

    monkeypatch.setattr(setup.subprocess, "Popen", launch)
    setup.open_signin()


def test_busy_profile_does_not_launch_or_touch_other_profile(profile, tmp_path, monkeypatch):
    session, directory = profile
    directory.mkdir()
    other = tmp_path / "other-profile"
    other.mkdir()
    cookie = other / "Cookies"
    cookie.write_text("preserve")
    with (directory / ".tink-worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert setup.setup_status()["status"] == "busy"
        monkeypatch.setattr(setup.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not launch"))
        with pytest.raises(RuntimeError, match="browser_profile_busy"):
            setup.open_signin()
    assert cookie.read_text() == "preserve"
    assert not session.exists()


def test_background_helper_exit_preserves_native_owner(profile, tmp_path):
    session, directory = profile
    binary = tmp_path / "native-chrome"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "profile=pathlib.Path(next(a.split('=',1)[1] for a in sys.argv if a.startswith('--user-data-dir=')))\n"
        "while not (profile/'quit').exists(): time.sleep(.05)\n"
    )
    binary.chmod(0o700)
    helper = subprocess.Popen(
        [sys.executable, "-m", "jev_ultrafast.browser_setup", "--chrome-path", str(binary)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    browser_pid = None
    try:
        assert select.select([helper.stdout], [], [], 10)[0]
        assert json.loads(helper.stdout.readline()) == {"status": "running"}
        data = json.loads((session / "session.json").read_text())
        browser_pid = data["pid"]
        assert setup.setup_status() == {"status": "running"}
        with pytest.raises(RuntimeError, match="browser_profile_busy"):
            with OwnedChrome(allow_launch=False):
                pytest.fail("manual browser must remain exclusive")
        helper.kill()
        helper.wait(timeout=5)
        assert setup.setup_status() == {"status": "running"}
        with pytest.raises(RuntimeError, match="browser_session_unavailable"):
            with OwnedChrome(allow_launch=False):
                pytest.fail("must not attach to or signal native Chrome")
        os.kill(browser_pid, 0)
        (directory / "quit").touch()
        deadline = time.monotonic() + 5
        while setup.setup_status()["status"] != "idle" and time.monotonic() < deadline:
            time.sleep(.05)
        assert setup.setup_status()["status"] == "idle"
        with pytest.raises(RuntimeError, match="browser_session_missing"):
            OwnedChrome(allow_launch=False).__enter__()
        assert not (session / "session.json").exists()
    finally:
        (directory / "quit").touch()
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=5)
        if browser_pid:
            try:
                os.kill(browser_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_requires_explicit_profile_paths(monkeypatch):
    monkeypatch.delenv("TINK_BROWSER_PROFILE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="browser_setup_unavailable"):
        setup.setup_status()


def test_unpublished_native_launch_is_reaped_and_leases_released(profile, monkeypatch):
    session, directory = profile
    monkeypatch.setattr(setup, "chrome_binary", lambda _binary: "/fake/chrome")
    process = Mock(pid=456, poll=Mock(return_value=None))
    process.wait.side_effect = lambda **_kwargs: setattr(process.poll, "return_value", 0)
    monkeypatch.setattr(setup.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(OwnedChrome, "_save_session", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        setup.open_signin()
    process.terminate.assert_called_once()
    assert setup.setup_status() == {"status": "idle"}
    assert not (session / "session.json").exists()


def test_exited_native_launch_does_not_report_running(profile, monkeypatch):
    session, _directory = profile
    monkeypatch.setattr(setup, "chrome_binary", lambda _binary: "/fake/chrome")
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)
    process = Mock(pid=456, poll=Mock(return_value=1))
    monkeypatch.setattr(setup.subprocess, "Popen", Mock(return_value=process))
    started = Mock()
    with pytest.raises(RuntimeError, match="chrome_start_failed"):
        setup.open_signin(started=started)
    started.assert_not_called()
    assert not (session / "session.json").exists()


def test_manual_status_rejects_recycled_pid_without_signalling(profile, monkeypatch):
    session, directory = profile
    session.mkdir()
    (session / "session.json").write_text(json.dumps({
        "pid": 123, "binary": "/fake/chrome", "profile_dir": str(directory),
        "browser_session": "a" * 32, "setup": True,
    }))
    monkeypatch.setattr(setup.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout="S unrelated-process")))
    monkeypatch.setattr(setup.os, "kill", lambda *a: pytest.fail("must not signal unrelated process"))
    with pytest.raises(RuntimeError, match="browser_session_unavailable"):
        setup.setup_status()
