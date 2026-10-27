"""Verified download artifacts; live tests use only our temporary Chrome and local HTTP server."""

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import downloads as module
from jev_ultrafast.browser import Browser
from jev_ultrafast.downloads import DownloadTracker
from jev_ultrafast.worker_browser import OwnedChrome


class FakeChrome:
    def __init__(self):
        self.on_event = None
        self.events = []
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        while self.events:
            event, body = self.events.pop(0)
            self.on_event(event, body)
        return {}


def begin(chrome, guid="owned-guid", name="report.csv"):
    chrome.events.append(("Browser.downloadWillBegin", {
        "guid": guid, "suggestedFilename": name, "url": "https://example.test/report",
    }))


def completed(chrome, content, guid="owned-guid", **extra):
    chrome.events.append(("Browser.downloadProgress", {
        "guid": guid, "state": "completed", "receivedBytes": len(content), "totalBytes": len(content), **extra,
    }))


def test_unsolicited_download_event_is_dispatched_without_losing_cdp_response():
    chrome = OwnedChrome()
    chrome.on_event = Mock()
    chrome.socket = Mock(recv=Mock(side_effect=[
        json.dumps({"method": "Browser.downloadWillBegin", "params": {"guid": "owned"}}),
        json.dumps({"id": 1, "result": {"product": "Chrome"}}),
    ]))
    assert chrome.call("Browser.getVersion") == {"product": "Chrome"}
    chrome.on_event.assert_called_once_with("Browser.downloadWillBegin", {"guid": "owned"})


def test_verified_manifest_uses_guid_paths_and_cannot_overwrite_duplicate_names(tmp_path):
    chrome = FakeChrome()
    with DownloadTracker(chrome, tmp_path, check=lambda: None) as tracker:
        for guid, content in [("first", b"first report"), ("second", b"second report")]:
            begin(chrome, guid, "../../report.csv")
            (tracker.directory / guid).write_bytes(content)
            completed(chrome, content, guid)
        tracker.poll()
        assert [item["name"] for item in tracker.files] == ["report.csv", "report.csv"]
        assert len({item["path"] for item in tracker.files}) == 2
        for item in tracker.files:
            content = Path(item["path"]).read_bytes()
            assert Path(item["path"]).parent == tracker.directory
            assert item["bytes"] == len(content)
            assert item["sha256"] == hashlib.sha256(content).hexdigest()
            assert item["source_url"] == "https://example.test/report"
        assert tracker.directory.parent == tmp_path
        assert tracker.directory.stat().st_mode & 0o777 == 0o700
    assert chrome.calls[-1] == ("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": False})
    assert chrome.on_event is None


@pytest.mark.parametrize("invalid", ["traversal", "symlink", "directory", "size", "unexpected_path"])
def test_unverified_or_unsafe_files_never_enter_manifest(tmp_path, invalid):
    chrome = FakeChrome()
    with pytest.raises(RuntimeError, match="download_verification_failed"):
        with DownloadTracker(chrome, tmp_path, check=lambda: None) as tracker:
            guid = "../escape" if invalid == "traversal" else "owned-guid"
            begin(chrome, guid)
            path = tracker.directory / "owned-guid"
            outside = tmp_path / "outside"
            outside.write_bytes(b"report")
            if invalid == "symlink":
                path.symlink_to(outside)
            elif invalid == "directory":
                path.mkdir()
            else:
                path.write_bytes(b"different" if invalid == "size" else b"report")
            if invalid == "unexpected_path":
                # Replacing the private directory with a symlink cannot move
                # verification to an attacker-selected location.
                renamed = tracker.directory.with_name("moved")
                tracker.directory.rename(renamed)
                tracker.directory.symlink_to(renamed, target_is_directory=True)
            completed(chrome, b"report", guid)
            tracker.poll()
    assert tracker.files == []


def test_completed_event_does_not_claim_a_file_before_it_exists(tmp_path):
    chrome = FakeChrome()
    with DownloadTracker(chrome, tmp_path, check=lambda: None) as tracker:
        begin(chrome)
        completed(chrome, b"report")
        tracker.poll()
        assert tracker.files == []
        (tracker.directory / "owned-guid").write_bytes(b"report")
        tracker.poll()
        tracker.poll()
        assert len(tracker.files) == 1


def test_canceled_download_is_not_a_completed_artifact(tmp_path):
    chrome = FakeChrome()
    with pytest.raises(RuntimeError, match="download_failed"):
        with DownloadTracker(chrome, tmp_path, check=lambda: None) as tracker:
            begin(chrome)
            chrome.events.append(("Browser.downloadProgress", {"guid": "owned-guid", "state": "canceled"}))
            tracker.poll()
    assert tracker.files == []


def test_wait_settles_late_start_events_and_obeys_execution_deadline(tmp_path, monkeypatch):
    chrome, now = FakeChrome(), [0.0]
    tracker = DownloadTracker(chrome, tmp_path, check=lambda: None)

    def sleep(seconds):
        now[0] += seconds
        if not tracker.items:
            begin(chrome)
        elif now[0] >= 1.5 and not tracker.items["owned-guid"].get("verified"):
            (tracker.directory / "owned-guid").write_bytes(b"report")
            completed(chrome, b"report")

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    with tracker:
        tracker.wait()
        assert now[0] >= 1.5 and len(tracker.files) == 1

    def deadline():
        if now[0] >= 2:
            raise TimeoutError("execution budget exhausted")

    with pytest.raises(TimeoutError):
        with DownloadTracker(chrome, tmp_path, check=deadline) as pending:
            begin(chrome)
            pending.wait()
    assert pending.files == []
    assert ("Browser.cancelDownload", {"guid": "owned-guid"}) in chrome.calls
    assert chrome.on_event is None


def test_cleanup_cancels_pending_downloads_even_when_one_cancel_fails(tmp_path):
    chrome = FakeChrome()
    with pytest.raises(RuntimeError, match="download_cleanup_failed"):
        with DownloadTracker(chrome, tmp_path, check=lambda: None) as tracker:
            begin(chrome, "one")
            begin(chrome, "two")
            tracker.poll()
            original = chrome.call

            def call(method, **params):
                result = original(method, **params)
                if method == "Browser.cancelDownload" and params["guid"] == "one":
                    raise RuntimeError("transport failed")
                return result

            chrome.call = call
    assert ("Browser.cancelDownload", {"guid": "two"}) in chrome.calls
    assert chrome.calls[-1] == ("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": False})
    assert chrome.on_event is None


@pytest.fixture
def attachment_server():
    payloads = {"/one": b"column\nfirst\n", "/two": b"column\nsecond\n", "/unsafe": b"unsafe name, safe storage\n"}
    slow = b"slow attachment\n" * 1024

    class Attachment(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                content = b'<title>Download fixture</title><a id="download" href="/one">Download</a>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            else:
                content = slow if self.path == "/slow" else payloads[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                name = "../../escape.csv" if self.path == "/unsafe" else "report.csv"
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                if self.path == "/slow":
                    for offset in range(0, len(content), 1024):
                        self.wfile.write(content[offset:offset + 1024])
                        self.wfile.flush()
                        time.sleep(0.1)
                else:
                    self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Attachment)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", {**payloads, "/slow": slow}
    finally:
        server.shutdown()
        server.server_close()


def _deadline_check(seconds=15):
    deadline = time.monotonic() + seconds

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture deadline")

    return check


def _click_download(browser, path):
    browser.call("Runtime.evaluate", expression=(
        f"document.querySelector('a').href={json.dumps(path)};document.querySelector('a').click()"
    ), userGesture=True)


@pytest.mark.skipif(os.environ.get("TINK_TEST_OWNED_CHROME") != "1", reason="Opt in to our temporary Chrome fixture")
def test_real_attachment_completion_duplicates_and_slow_download(tmp_path, monkeypatch, attachment_server):
    monkeypatch.delenv("TINK_BROWSER_SESSION_DIR", raising=False)
    monkeypatch.delenv("TINK_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    base, payloads = attachment_server
    with OwnedChrome() as chrome:
        browser = Browser(base, transport=chrome.call)
        try:
            with DownloadTracker(chrome, tmp_path, check=_deadline_check()) as tracker:
                for path in ["/one", "/two", "/unsafe", "/slow"]:
                    _click_download(browser, path)
                    tracker.wait()
                assert len(tracker.files) == 4
                assert tracker.files[0]["name"] == tracker.files[1]["name"] == "report.csv"
                assert len({item["path"] for item in tracker.files}) == 4
                for item in tracker.files:
                    content = payloads[item["source_url"].removeprefix(base)]
                    assert Path(item["path"]).read_bytes() == content
                    assert Path(item["path"]).parent == tracker.directory
                    assert item["sha256"] == hashlib.sha256(content).hexdigest()
                    assert item["bytes"] == len(content)
                    assert "/" not in item["name"] and "\\" not in item["name"]
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("TINK_TEST_OWNED_CHROME") != "1", reason="Opt in to our temporary Chrome fixture")
def test_real_cancellation_removes_partial_and_denies_later_downloads(tmp_path, monkeypatch, attachment_server):
    monkeypatch.delenv("TINK_BROWSER_SESSION_DIR", raising=False)
    monkeypatch.delenv("TINK_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    base, _ = attachment_server
    check = _deadline_check()
    with OwnedChrome() as chrome:
        browser = Browser(base, transport=chrome.call)
        try:
            with pytest.raises(InterruptedError):
                with DownloadTracker(chrome, tmp_path, check=check) as tracker:
                    _click_download(browser, "/slow")
                    while not tracker.items:
                        tracker.poll()
                        time.sleep(0.02)
                    raise InterruptedError("user cancelled")
            assert tracker.files == [] and chrome.on_event is None
            while list(tracker.directory.iterdir()):
                check()
                chrome.call("Browser.getVersion")
                time.sleep(0.02)
            _click_download(browser, "/one")
            time.sleep(0.2)
            chrome.call("Browser.getVersion")
            assert list(tracker.directory.iterdir()) == []
        finally:
            browser.close()
