"""A disposable Chrome owned by one worker; never attach to an existing profile."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    def __init__(self, binary=None):
        self.binary = binary
        self.profile = None
        self.process = None
        self.socket = None
        self.sequence = 0

    def __enter__(self):
        # The host owns this process group, including Chrome and its child processes.
        if sys.platform == "win32":
            raise RuntimeError("unsupported_platform")
        binary = chrome_binary(self.binary)
        self.profile = tempfile.TemporaryDirectory(prefix="tink-browser-")
        args = [
            binary, f"--user-data-dir={self.profile.name}", "--remote-debugging-port=0",
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
            port_file = Path(self.profile.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not port_file.is_file():
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("chrome_start_failed")
                time.sleep(0.05)
            port, socket_path, *_ = port_file.read_text().splitlines()
            if not port.isdigit() or not socket_path.startswith("/devtools/browser/"):
                raise RuntimeError("chrome_start_failed")
            self.socket = connect(
                f"ws://127.0.0.1:{int(port)}{socket_path}", proxy=None, open_timeout=5, close_timeout=1,
                max_size=8 * 1024 * 1024,
            )
            self.call("Browser.setDownloadBehavior", behavior="deny")
            return self
        except BaseException:
            self.close()
            raise

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
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        if self.process is not None:
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

    def __exit__(self, *_args):
        self.close()
