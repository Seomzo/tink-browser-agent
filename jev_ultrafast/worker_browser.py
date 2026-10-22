"""Owned Chrome: disposable by default, or retained under an exclusive profile lease."""

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from websockets.sync.client import connect


def chrome_binary(configured=None):
    configured = configured or os.environ.get("TINK_BROWSER_CHROME_PATH")
    candidates = [configured] if configured else [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise RuntimeError("chrome_unavailable")


class OwnedChrome:
    def __init__(self, binary=None, *, allow_launch=True):
        self.binary = binary
        self.profile = None
        self.profile_lock = None
        self.process = None
        self.socket = None
        self.sequence = 0
        self.allow_launch = allow_launch
        self.persistent = bool(os.environ.get("TINK_BROWSER_SESSION_DIR"))
        self.session_data = {}
        self.session_lock = None
        self.session_dir = None
        self.profile_dir = None

    def __enter__(self):
        # Disposable Chrome shares the task group; live Chrome has its own group.
        if sys.platform == "win32":
            raise RuntimeError("unsupported_platform")
        if self.persistent:
            return self._enter_persistent()
        binary = chrome_binary(self.binary)
        saved = os.environ.get("TINK_BROWSER_PROFILE_DIR")
        if saved:
            profile_dir = Path(saved)
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile_lock = (profile_dir / '.tink-worker.lock').open('a')
            try:
                fcntl.flock(self.profile_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.profile_lock.close()
                self.profile_lock = None
                raise RuntimeError("browser_profile_busy") from None
            # A previous process may have been killed; never attach using an old port.
            (profile_dir / 'DevToolsActivePort').unlink(missing_ok=True)
        else:
            self.profile = tempfile.TemporaryDirectory(prefix="tink-browser-")
            profile_dir = Path(self.profile.name)
        args = [
            binary, f"--user-data-dir={profile_dir}", "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--disable-sync", "--disable-extensions", "--disable-background-networking",
            "--disable-component-update", "--disable-default-apps", "--password-store=basic",
            "about:blank",
        ]
        if os.environ.get("TINK_BROWSER_HEADLESS") == "1":
            args.insert(1, "--headless=new")
        try:
            # No credentials need to reach Chrome's environment.
            chrome_env = {k: v for k, v in os.environ.items() if not any(
                word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )}
            self.process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=chrome_env,
            )
            port_file = profile_dir / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not port_file.is_file():
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("chrome_start_failed")
                time.sleep(0.05)
            port, socket_path, *_ = port_file.read_text().splitlines()
            if not port.isdigit() or not socket_path.startswith("/devtools/browser/"):
                raise RuntimeError("chrome_start_failed")
            self.socket = connect(
                f"ws://127.0.0.1:{int(port)}{socket_path}", proxy=None, open_timeout=15, close_timeout=1,
                max_size=8 * 1024 * 1024,
            )
            self.call("Browser.setDownloadBehavior", behavior="deny")
            return self
        except BaseException:
            self.close()
            raise

    def _enter_persistent(self):
        # The session lease is held for a whole task, including human/approval waits.
        self.session_dir = Path(os.environ["TINK_BROWSER_SESSION_DIR"]).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.session_dir, 0o700)
        self.session_lock = (self.session_dir / "session.lock").open("a")
        try:
            fcntl.flock(self.session_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            saved = os.environ.get("TINK_BROWSER_PROFILE_DIR")
            self.profile_dir = Path(saved).resolve() if saved else self.session_dir / "browser-data"
            self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.profile_dir, 0o700)
            self.profile_lock = (self.profile_dir / ".tink-worker.lock").open("a")
            fcntl.flock(self.profile_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = self.session_dir / "session.json"
            if metadata.exists():
                try:
                    self.session_data = json.loads(metadata.read_text())
                    if (not isinstance(self.session_data, dict)
                            or self.session_data.get("profile_dir") != str(self.profile_dir)
                            or type(self.session_data.get("pid")) is not int
                            or not isinstance(self.session_data.get("binary"), str)
                            or not re.fullmatch(r"[0-9a-f]{32}", str(self.session_data.get("browser_session", "")))):
                        raise ValueError()
                except (ValueError, OSError):
                    raise RuntimeError("browser_session_unavailable") from None
                if self._owned_process_alive():
                    self._connect_persistent()
                    return self
                metadata.unlink()
                self.session_data = {}
            if not self.allow_launch:
                raise RuntimeError("browser_session_missing")
            binary = chrome_binary(self.binary)
            port_file = self.profile_dir / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            tmp = self.session_dir / "tmp"
            tmp.mkdir(exist_ok=True, mode=0o700)
            chrome_env = {k: v for k, v in os.environ.items() if not any(
                word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )}
            chrome_env["TMPDIR"] = str(tmp)
            browser_session = uuid.uuid4().hex
            args = [binary, f"--user-data-dir={self.profile_dir}", "--remote-debugging-port=0",
                    f"--tink-browser-session={browser_session}",
                    "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                    "--disable-sync", "--disable-extensions", "--disable-background-networking",
                    "--disable-component-update", "--disable-default-apps", "--password-store=basic", "about:blank"]
            if os.environ.get("TINK_BROWSER_HEADLESS") == "1":
                args.insert(1, "--headless=new")
            self.process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=chrome_env, cwd=self.session_dir, start_new_session=True,
            )
            self.session_data = {"pid": self.process.pid, "binary": binary,
                                 "profile_dir": str(self.profile_dir), "browser_session": browser_session,
                                 "target_id": None}
            self._save_session()
            self._connect_persistent()
            return self
        except BaseException as exc:
            # Before publication only this Popen knows the launch. Ordinary
            # interruption must not strand a browser that cannot be resumed.
            if self.process is not None and not (self.session_dir / "session.json").exists():
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.close()
            if isinstance(exc, BlockingIOError):
                raise RuntimeError("browser_profile_busy") from None
            raise

    def _owned_process_alive(self):
        pid = self.session_data["pid"]
        if pid <= 0:
            raise RuntimeError("browser_session_unavailable")
        if self.process is not None and self.process.poll() is not None:
            return False
        # Launchers may exec a different binary (e.g. google-chrome on Linux).
        # A nonce and the dedicated profile identify this launch, not a recycled PID.
        found = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "args="],
                               capture_output=True, text=True, timeout=3)
        if found.returncode == 1 and not found.stdout.strip():
            return False
        state, _, command = found.stdout.strip().partition(" ")
        command = command.lstrip()
        if state.startswith("Z"):
            return False
        profile_flag = "--user-data-dir=" + str(self.profile_dir)
        nonce_flag = "--tink-browser-session=" + self.session_data["browser_session"]
        if (found.returncode != 0
                or not re.search(re.escape(profile_flag) + r"(?:\s|$)", command)
                or not re.search(re.escape(nonce_flag) + r"(?:\s|$)", command)
                or "--remote-debugging-port=0" not in command):
            raise RuntimeError("browser_session_unavailable")
        return True

    def _connect_persistent(self):
        port_file = self.profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.is_file():
            if not self._owned_process_alive() or time.monotonic() >= deadline:
                raise RuntimeError("chrome_start_failed")
            time.sleep(0.05)
        lines = port_file.read_text().splitlines()
        if (len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535
                or not re.fullmatch(r"/devtools/browser/[a-zA-Z0-9-]+", lines[1])):
            raise RuntimeError("browser_session_unavailable")
        endpoint = f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
        if self.session_data.get("endpoint", endpoint) != endpoint:
            raise RuntimeError("browser_session_unavailable")
        self.socket = connect(endpoint, proxy=None, open_timeout=3, close_timeout=1, max_size=8 * 1024 * 1024)
        self.call("Browser.getVersion")
        self.call("Browser.setDownloadBehavior", behavior="deny")
        self.session_data["endpoint"] = endpoint
        self._save_session()

    def _save_session(self):
        pending = self.session_dir / "session.json.tmp"
        pending.write_text(json.dumps(self.session_data))
        os.chmod(pending, 0o600)
        pending.replace(self.session_dir / "session.json")

    def retain_target(self, target):
        self.session_data["target_id"] = target
        self._save_session()

    def shutdown(self):
        """Explicit close only, while the exclusive profile/session lease is held."""
        if not self.persistent:
            self.close()
            return
        if self.socket is not None:
            try:
                self.sequence += 1
                self.socket.send(json.dumps({"id": self.sequence, "method": "Browser.close"}))
            except Exception:
                pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + 3
            signalled = False
            while True:
                try:
                    alive = self._owned_process_alive()
                except RuntimeError:
                    # During exit, ps can briefly lose argv before the PID vanishes.
                    # Wait through that state, but never signal an unverified PID.
                    if time.monotonic() >= deadline:
                        raise
                else:
                    if not alive:
                        if self.process is not None:
                            self.process.wait(timeout=1)
                        (self.session_dir / "session.json").unlink(missing_ok=True)
                        return
                    if sig is not None and not signalled:
                        try:
                            os.kill(self.session_data["pid"], sig)
                        except ProcessLookupError:
                            pass
                        signalled = True
                    if time.monotonic() >= deadline:
                        break
                time.sleep(0.05)
        raise RuntimeError("browser_close_failed")

    def call(self, method, session_id=None, **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        if session_id is not None:
            request["sessionId"] = session_id
        self.socket.send(json.dumps(request))
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("browser_request_timeout")
            reply = json.loads(self.socket.recv(timeout=remaining))
            if reply.get("id") != self.sequence:
                continue
            if "error" in reply:
                raise RuntimeError("browser_request_failed")
            return reply.get("result", {})

    def close(self):
        if self.persistent:
            # Disconnecting a task must not close its tab or detached Chrome.
            if self.socket is not None:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            self.process = None
            for lock in (self.profile_lock, self.session_lock):
                if lock is not None:
                    lock.close()
            self.profile_lock = self.session_lock = None
            return
        closing = False
        if self.socket is not None:
            try:
                # Browser.close flushes cookies/storage; SIGTERM can lose recent logins.
                self.sequence += 1
                self.socket.send(json.dumps({"id": self.sequence, "method": "Browser.close"}))
                closing = True
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        if self.process is not None:
            if closing:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
        if self.profile is not None:
            self.profile.cleanup()
            self.profile = None

        if self.profile_lock is not None:
            self.profile_lock.close()
            self.profile_lock = None

    def __exit__(self, *_args):
        self.close()
