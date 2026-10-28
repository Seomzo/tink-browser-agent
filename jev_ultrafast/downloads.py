"""Task-scoped Chrome downloads with verified, uniquely named local artifacts."""

import hashlib
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path

_GUID = re.compile(r"[a-zA-Z0-9_-]{1,128}")


def recover_downloads(chrome):
    """Cancel orphaned transfers under the already-verified browser/session lease."""
    journal = chrome.session_data.get("downloads")
    if journal is None:
        return
    root = os.environ.get("TINK_BROWSER_DOWNLOAD_DIR")
    if (not root or not isinstance(journal, dict)
            or journal.get("browser_session") != chrome.session_data.get("browser_session")
            or not isinstance(journal.get("directory"), str)
            or not isinstance(journal.get("active_guids"), list)
            or any(not isinstance(guid, str) or not _GUID.fullmatch(guid) for guid in journal["active_guids"])):
        raise RuntimeError("download_cleanup_failed")
    directory = Path(journal["directory"])
    if (not directory.is_absolute() or directory.is_symlink() or directory.resolve() != directory
            or directory.parent != Path(root).expanduser().resolve() or not directory.name.startswith("task-")
            or not directory.is_dir()):
        raise RuntimeError("download_cleanup_failed")
    failed = False
    attempted = set()
    deadline = time.monotonic() + 5
    try:
        # Changing policy does not cancel existing downloads. Drain begin events,
        # then recover GUIDs even if the old worker died before reading an event.
        chrome.call("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=True)
        while True:
            partials = set()
            for path in directory.glob("*.crdownload"):
                guid = path.name.removesuffix(".crdownload")
                if path.is_symlink() or not _GUID.fullmatch(guid):
                    raise RuntimeError("download_cleanup_failed")
                partials.add(guid)
            active = set(journal["active_guids"]) | partials
            for guid in active - attempted:
                attempted.add(guid)
                try:
                    chrome.call("Browser.cancelDownload", guid=guid)
                    if guid in journal["active_guids"]:
                        journal["active_guids"].remove(guid)
                        chrome._save_session()
                except Exception:
                    failed = True
                if time.monotonic() >= deadline:
                    failed = True
                    break
            if failed or not active:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("download_cleanup_failed")
            chrome.call("Browser.getVersion")
            time.sleep(0.05)
    finally:
        # Keep the journal on any failure, so the next reconnect can retry.
        chrome.call("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=False)
    if failed:
        raise RuntimeError("download_cleanup_failed")
    chrome.session_data.pop("downloads", None)
    chrome._save_session()


def _filename(value):
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).strip(" .")
    return name[:200] or "download"


class DownloadTracker:
    def __init__(self, chrome, directory, *, check):
        self.chrome, self.check = chrome, check
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix="task-", dir=root))
        self.files = []
        self.items = {}
        self.error = None
        self.previous_handler = chrome.on_event
        self.persistent = getattr(chrome, "persistent", False) is True

    def __enter__(self):
        self.chrome.on_event = self._event
        try:
            if self.persistent:
                if self.chrome.session_data.get("downloads") is not None:
                    raise RuntimeError("download_cleanup_failed")
                self.chrome.session_data["downloads"] = {
                    "browser_session": self.chrome.session_data["browser_session"],
                    "directory": str(self.directory), "active_guids": [],
                }
                self.chrome._save_session()
            self.chrome.call("Browser.setDownloadBehavior", behavior="allowAndName",
                             downloadPath=str(self.directory), eventsEnabled=True)
        except BaseException:
            try:
                self.chrome.call("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=False)
            finally:
                self.chrome.on_event = self.previous_handler
            raise
        return self

    def _event(self, method, params):
        if self.previous_handler is not None:
            self.previous_handler(method, params)
        if method not in {"Browser.downloadWillBegin", "Browser.downloadProgress"}:
            return
        guid = params.get("guid")
        if not isinstance(guid, str) or not _GUID.fullmatch(guid):
            self.error = "download_verification_failed"
            return
        if method == "Browser.downloadWillBegin":
            if guid in self.items:
                self.error = "download_verification_failed"
                return
            self.items[guid] = {"state": "inProgress", "name": _filename(params.get("suggestedFilename", "")),
                                "source_url": str(params.get("url", ""))[:4096]}
        elif guid in self.items:
            self.items[guid].update(state=params.get("state"), received=params.get("receivedBytes"),
                                    total=params.get("totalBytes"))
        if self.persistent:
            self.chrome.session_data["downloads"]["active_guids"] = [
                guid for guid, item in self.items.items() if item["state"] == "inProgress"
            ]
            self.chrome._save_session()

    def _verify(self, guid, item):
        path = self.directory / guid
        if self.directory.resolve() != self.directory or path.resolve().parent != self.directory:
            raise RuntimeError("download_verification_failed")
        received, total = item.get("received"), item.get("total")
        if any(type(value) not in {int, float} or not math.isfinite(value) or value < 0
               or value != int(value) for value in (received, total)):
            raise RuntimeError("download_verification_failed")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            # CDP completion can precede the final rename becoming visible.
            return
        except OSError:
            raise RuntimeError("download_verification_failed") from None
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            os.close(descriptor)
            raise RuntimeError("download_verification_failed")
        with os.fdopen(descriptor, "rb") as source:
            if before.st_size != received or (total and total != received):
                raise RuntimeError("download_verification_failed")
            digest, size = hashlib.sha256(), 0
            while chunk := source.read(1024 * 1024):
                self.check()
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(source.fileno())
            if (size != received or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or path.stat(follow_symlinks=False).st_ino != after.st_ino):
                raise RuntimeError("download_verification_failed")
        self.files.append({"path": str(path), "name": item["name"], "source_url": item["source_url"],
                           "bytes": size, "sha256": digest.hexdigest()})
        item["verified"] = True

    def poll(self):
        self.check()
        # Responses are ordered with the pending browser events on this socket.
        self.chrome.call("Browser.getVersion")
        if self.error:
            raise RuntimeError(self.error)
        for guid, item in list(self.items.items()):
            if item["state"] == "canceled":
                raise RuntimeError("download_failed")
            if item["state"] == "completed" and not item.get("verified"):
                self._verify(guid, item)
        return self.files

    def wait(self):
        # Only finishing a task pays this bounded window for a just-clicked
        # download's first event; normal action polling never sleeps.
        settle_until = time.monotonic() + 1
        while True:
            self.poll()
            if all(item.get("verified") for item in self.items.values()) and time.monotonic() >= settle_until:
                return self.files
            time.sleep(0.05)

    def __exit__(self, exc_type, *_args):
        if self.persistent:
            try:
                recover_downloads(self.chrome)
            except Exception:
                if exc_type is None:
                    raise RuntimeError("download_cleanup_failed") from None
            finally:
                self.chrome.on_event = self.previous_handler
            return
        failed = False
        try:
            # Deny new requests before canceling those already in flight.
            try:
                self.chrome.call("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=True)
                self.chrome.call("Browser.getVersion")
            except Exception:
                failed = True
            for guid, item in list(self.items.items()):
                if item["state"] == "inProgress":
                    try:
                        self.chrome.call("Browser.cancelDownload", guid=guid)
                    except Exception:
                        failed = True
            try:
                self.chrome.call("Browser.setDownloadBehavior", behavior="deny", eventsEnabled=False)
            except Exception:
                failed = True
        finally:
            self.chrome.on_event = self.previous_handler
        if failed and exc_type is None:
            raise RuntimeError("download_cleanup_failed")
