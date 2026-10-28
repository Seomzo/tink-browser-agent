"""Recovery after abrupt worker death uses only this session's download journal."""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import downloads
from jev_ultrafast.worker import Protocol, run_task, validate_request
from jev_ultrafast.worker_browser import OwnedChrome

GUID = "d13c294b-0167-43f2-ae3a-8aac03212483"
SESSION = "a" * 32


def journal_chrome(directory):
    return SimpleNamespace(
        persistent=True, on_event=None, call=Mock(return_value={}), _save_session=Mock(),
        session_data={"browser_session": SESSION, "downloads": {
            "browser_session": SESSION, "directory": str(directory), "active_guids": [GUID],
        }},
    )


def test_download_ownership_is_saved_before_enabling_and_before_event_processing_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_DOWNLOAD_DIR", str(tmp_path))
    chrome = journal_chrome(tmp_path / "unused")
    chrome.session_data.pop("downloads")
    persisted = []
    chrome._save_session.side_effect = lambda: persisted.append(deepcopy(chrome.session_data))

    def call(method, **params):
        if method == "Browser.setDownloadBehavior" and params["behavior"] == "allowAndName":
            assert persisted[-1]["downloads"]["directory"] == params["downloadPath"]
            assert persisted[-1]["downloads"]["browser_session"] == SESSION
        return {}

    chrome.call.side_effect = call
    with downloads.DownloadTracker(chrome, tmp_path, check=lambda: None):
        chrome.on_event("Browser.downloadWillBegin", {
            "guid": GUID, "url": "https://example.test/report", "suggestedFilename": "report.csv",
        })
        assert GUID in persisted[-1]["downloads"]["active_guids"]
    assert not chrome.session_data.get("downloads")


@pytest.mark.parametrize("invalid", ["outside_root", "symlink", "different_session"])
def test_recovery_rejects_foreign_download_journal_without_canceling_or_touching_files(tmp_path, monkeypatch, invalid):
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setenv("TINK_BROWSER_DOWNLOAD_DIR", str(root))
    directory = root / "task-saved"
    directory.mkdir(mode=0o700)
    if invalid == "outside_root":
        directory = tmp_path / "task-foreign"
        directory.mkdir()
    elif invalid == "symlink":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        directory.rmdir()
        directory.symlink_to(foreign, target_is_directory=True)
    file = directory / (GUID + ".crdownload")
    file.write_bytes(b"foreign partial file")
    chrome = journal_chrome(directory)
    if invalid == "different_session":
        chrome.session_data["downloads"]["browser_session"] = "b" * 32
    saved = deepcopy(chrome.session_data)
    with pytest.raises(RuntimeError, match="download_cleanup_failed"):
        downloads.recover_downloads(chrome)
    assert chrome.session_data == saved
    assert not any(call.args[0] == "Browser.cancelDownload" for call in chrome.call.call_args_list)
    assert file.read_bytes() == b"foreign partial file"


def test_failed_cancel_preserves_download_journal_for_next_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_DOWNLOAD_DIR", str(tmp_path))
    directory = tmp_path / "task-saved"
    directory.mkdir(mode=0o700)
    chrome = journal_chrome(directory)
    saved = deepcopy(chrome.session_data)

    def call(method, **params):
        if method == "Browser.cancelDownload":
            raise RuntimeError("transport unavailable")
        return {}

    chrome.call.side_effect = call
    with pytest.raises(RuntimeError, match="download_cleanup_failed"):
        downloads.recover_downloads(chrome)
    assert chrome.session_data == saved


def test_read_only_tab_listing_never_enables_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.setenv("TINK_BROWSER_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    chrome = journal_chrome(tmp_path / "unused")
    chrome.session_data.pop("downloads")
    chrome.session_data["target_id"] = "owned-tab"
    chrome.call.return_value = {"targetInfos": [
        {"targetId": "owned-tab", "type": "page", "url": "https://example.test/", "title": "Fixture"},
    ]}
    context = Mock(__enter__=Mock(return_value=chrome), __exit__=Mock(return_value=False))
    no_browser = Mock(side_effect=AssertionError("Tab listing must not construct an agent or page controller"))
    request = validate_request({"type": "start", "protocol_version": 1, "action": "tabs"})
    result = run_task(request, Protocol(io.StringIO(), io.StringIO()), chrome_factory=Mock(return_value=context),
                      browser_factory=no_browser, agent_factory=no_browser)
    assert result["status"] == "completed" and result["stop_reason"] == "tabs_listed"
    assert not any(call.kwargs.get("behavior") == "allowAndName" for call in chrome.call.call_args_list)
    assert not (tmp_path / "downloads").exists()


@pytest.fixture
def slow_attachment():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/slow":
                content = b'<title>Download recovery fixture</title><a href="/slow">Download</a>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="slow.bin"')
            self.send_header("Content-Length", str(128 * 65536))
            self.end_headers()
            try:
                for _ in range(128):
                    self.wfile.write(b"x" * 65536)
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.environ.get("TINK_TEST_OWNED_CHROME") != "1", reason="Opt in to our temporary Chrome fixture")
@pytest.mark.parametrize("missed_event", [False, True])
def test_persistent_chrome_reconnect_cancels_download_after_worker_sigkill(
    tmp_path, monkeypatch, slow_attachment, missed_event,
):
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("TINK_BROWSER_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.setenv("TINK_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("TINK_BROWSER_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    ready = tmp_path / "ready.json"
    child = f'''import json, time
from pathlib import Path
from jev_ultrafast.worker_browser import OwnedChrome
from jev_ultrafast.browser import Browser
from jev_ultrafast.downloads import DownloadTracker
with OwnedChrome() as chrome:
    browser = Browser({slow_attachment!r}, transport=chrome.call)
    with DownloadTracker(chrome, {str(tmp_path / 'downloads')!r}, check=lambda: None) as tracker:
        if {missed_event!r}:
            # Model death before the asynchronous event reaches the tracker.
            chrome.on_event = None
        browser.call('Runtime.evaluate', expression="document.querySelector('a').click()", userGesture=True)
        if not {missed_event!r}:
            while not tracker.items:
                tracker.poll()
                time.sleep(.02)
        Path({str(ready)!r}).write_text(json.dumps({{'directory': str(tracker.directory)}}))
        time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True)
    metadata = tmp_path / "session" / "session.json"
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert process.poll() is None, process.stderr.read()
            assert time.monotonic() < deadline, "The local download fixture did not start"
            time.sleep(.02)
        directory = Path(json.loads(ready.read_text())["directory"])
        while not any(path.stat().st_size for path in directory.glob("*.crdownload")):
            assert time.monotonic() < deadline, "The local attachment did not start writing"
            time.sleep(.02)
        saved = json.loads(metadata.read_text())
        assert bool(saved["downloads"]["active_guids"]) is not missed_event
        process.kill()
        process.wait(timeout=3)
        assert process.returncode == -signal.SIGKILL
        with OwnedChrome(allow_launch=False) as chrome:
            assert chrome.session_data["pid"] == saved["pid"]
            assert chrome.session_data["browser_session"] == saved["browser_session"]
            assert not chrome.session_data.get("downloads")
            deadline = time.monotonic() + 3
            while list(directory.iterdir()):
                assert time.monotonic() < deadline, "Killed worker's attachment kept writing after recovery"
                chrome.call("Browser.getVersion")
                time.sleep(.02)
            assert subprocess.run(["ps", "-p", str(saved["pid"])], stdout=subprocess.DEVNULL).returncode == 0
            chrome.shutdown()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stderr.close()
        if metadata.exists():
            saved = json.loads(metadata.read_text())
            command = subprocess.run(["ps", "-ww", "-p", str(saved["pid"]), "-o", "args="],
                                     capture_output=True, text=True).stdout
            if f"--user-data-dir={tmp_path / 'profile'}" in command:
                try:
                    os.killpg(saved["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
