"""User-controlled sign-in in the same isolated profile used by browser tasks."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .worker_browser import OwnedChrome, chrome_binary


def _paths():
    session = os.environ.get("TINK_BROWSER_SESSION_DIR")
    profile = os.environ.get("TINK_BROWSER_PROFILE_DIR")
    if not session or not profile or sys.platform == "win32":
        raise RuntimeError("browser_setup_unavailable")
    return Path(session).resolve(), Path(profile).resolve()


def _manual_alive(data, profile):
    if (not isinstance(data, dict) or data.get("setup") is not True
            or data.get("profile_dir") != str(profile)
            or type(data.get("pid")) is not int or data["pid"] <= 0
            or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("browser_session", "")))):
        raise RuntimeError("browser_session_unavailable")
    found = subprocess.run(["ps", "-ww", "-p", str(data["pid"]), "-o", "stat=", "-o", "args="],
                           capture_output=True, text=True, timeout=3)
    if found.returncode == 1 and not found.stdout.strip():
        return False
    state, _, command = found.stdout.strip().partition(" ")
    if state.startswith("Z"):
        return False
    flags = (f"--user-data-dir={profile}", f"--tink-browser-session={data['browser_session']}")
    if found.returncode or any(not re.search(re.escape(flag) + r"(?:\s|$)", command) for flag in flags):
        raise RuntimeError("browser_session_unavailable")
    return True


def setup_status():
    session, profile = _paths()
    metadata = session / "session.json"
    if metadata.exists():
        data = json.loads(metadata.read_text())
        if isinstance(data, dict) and data.get("setup") is True and _manual_alive(data, profile):
            return {"status": "running"}
    # A running task may not have published session metadata yet.
    for lock_path in (session / "session.lock", profile / ".tink-worker.lock"):
        if not lock_path.exists():
            continue
        with lock_path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "reason": "browser_profile_busy"}
    return {"status": "idle"}


def open_signin(binary=None, *, started=None):
    """Hold both worker leases until native Chrome exits; never automate sign-in."""
    session, profile = _paths()
    if setup_status()["status"] != "idle":
        raise RuntimeError("browser_profile_busy")
    chrome = OwnedChrome(binary, allow_launch=False)
    process = None
    published = False
    try:
        try:
            chrome.__enter__()
        except RuntimeError as exc:
            if str(exc) != "browser_session_missing":
                raise
            # __enter__ releases its leases on the missing-session path. Recheck
            # metadata under both leases so a worker winning that gap stays owner.
            chrome.session_dir, chrome.profile_dir = session, profile
            for directory in (session, profile):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            chrome.session_lock = (session / "session.lock").open("a")
            fcntl.flock(chrome.session_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            chrome.profile_lock = (profile / ".tink-worker.lock").open("a")
            fcntl.flock(chrome.profile_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (session / "session.json").exists():
                raise RuntimeError("browser_profile_busy")
        else:
            # shutdown leaves the leases held. No task can race the handoff.
            chrome.shutdown()
            if chrome.socket is not None:
                chrome.socket.close()
                chrome.socket = None
        executable = chrome_binary(binary)
        nonce = uuid.uuid4().hex
        environment = {key: value for key, value in os.environ.items() if not any(
            word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        )}
        if sys.platform == "linux":
            environment["TMPDIR"] = "/tmp"
        process = subprocess.Popen(
            [executable, f"--user-data-dir={profile}", f"--tink-browser-session={nonce}",
             "--no-default-browser-check", "chrome://settings/people"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=environment, cwd=session, start_new_session=True,
        )
        chrome.session_data = {"pid": process.pid, "binary": executable, "profile_dir": str(profile),
                               "browser_session": nonce, "target_id": None, "setup": True}
        # Existing worker identity checks reject a live non-debugging owner if this
        # helper is killed. After Chrome exits the stale metadata can be reclaimed.
        chrome._save_session()
        published = True
        time.sleep(0.2)
        if process.poll() is not None:
            raise RuntimeError("chrome_start_failed")
        if started:
            started({"status": "running"})
        process.wait()
    except BlockingIOError:
        raise RuntimeError("browser_profile_busy") from None
    finally:
        if process is not None and not published and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        # Do not kill the user's browser or discard its ownership on helper exit.
        if process is not None and process.poll() is not None:
            (session / "session.json").unlink(missing_ok=True)
        chrome.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--chrome-path")
    args = parser.parse_args()
    try:
        if args.status:
            print(json.dumps(setup_status()), flush=True)
        else:
            open_signin(args.chrome_path, started=lambda result: print(json.dumps(result), flush=True))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) else "browser_setup_unavailable"
        print(json.dumps({"status": "busy" if reason == "browser_profile_busy" else "error",
                          "reason": reason}), flush=True)


if __name__ == "__main__":
    main()
