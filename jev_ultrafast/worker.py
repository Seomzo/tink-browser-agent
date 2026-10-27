"""One supervised browser task over bounded JSONL. Stdout is protocol-only."""

import argparse
import json
import math
import os
import queue
import signal
import sys
import threading
import time
from contextlib import ExitStack
from urllib.parse import urlsplit

from .agent import Agent
from .browser import Browser, StalePage
from .questions import VERIFICATION_REQUEST
from .worker_browser import OwnedChrome, chrome_binary

PROTOCOL_VERSION = 1
APPROVAL_ALLOWANCE_SECONDS = 180
MAX_LINE_BYTES = 262144  # Accommodates JSON-escaped Unicode within the validated text/path limits.
SUMMARIES = {
    "human_input_unavailable": "Verification needs human input, but no response was received.",
    "model_done": "The browser model reported completion. Review the page evidence to verify the goal.",
    "model_blocked": "The browser model could not find a supported next action.",
    "no_progress": "The browser stopped after repeated actions without a visible change.",
    "step_limit": "The browser reached its action limit. Partial evidence is available.",
    "decision_limit": "The browser reached its decision limit. Partial evidence is available.",
    "approval_denied": "A proposed browser action was denied. No further action was executed.",
    "approval_timeout": "The browser task exhausted its approval waiting allowance. No pending action was executed.",
    "credential_material": "The worker refused to send a configured credential through a browser action.",
    "cancelled": "The browser task was cancelled.",
    "host_disconnected": "The supervising host disconnected.",
    "deadline_exceeded": "The browser task reached its time limit.",
    "invalid_request": "The browser request did not match protocol version 1.",
    "invalid_response": "The host returned an invalid action approval.",
    "worker_error": "The browser worker failed. Review partial evidence before retrying.",
    "browser_session_missing": "No live browser session exists. Supply a starting URL to open one.",
    "browser_target_closed": "The retained browser tab was closed. Supply a starting URL to open a new tab.",
    "browser_profile_busy": "Another browser task owns this profile. Wait for it to finish or cancel it first.",
    "browser_session_unavailable": "The saved browser session could not be verified. No browser was attached.",
    "action_unavailable": "The same action was rejected three times without a page change. The page remains available.",
    "multiple_new_tabs": "The click opened multiple browser tabs. No tab was selected; inspect them before continuing.",
    "browser_closed": "The live browser session is closed. Saved site data was retained.",
    "tabs_listed": "Open tabs in this Tink browser are listed. Use an observed tab_id to continue there.",
    "verification_required": ("This tab needs a verification code. Retrieve it through authorized email access "
                              "or ask the user, then resume this tab with verification_response. "
                              "The browser remains open."),
    "verification_not_requested": ("The selected page does not match a pending verification-code request. "
                                   "No code was entered; inspect the current page before continuing."),
    "download_failed": "A requested file download did not complete. Review partial evidence before retrying.",
    "download_verification_failed": ("The downloaded file could not be verified. "
                                     "It was not reported as a completed artifact."),
    "download_cleanup_failed": ("The browser could not finish download cleanup. "
                                "Review the files and browser before continuing."),
    "downloads_cleaned": "Pending downloads from the stopped task were canceled. The browser remains open.",
}


class Stopped(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def validate_request(request):
    if not isinstance(request, dict) or request.get("type") != "start" or request.get("protocol_version") != 1:
        raise ValueError("invalid_request")
    action = request.get("action", "run")
    goal, url = request.get("goal", ""), request.get("start_url")
    if (action not in {"run", "tabs"} or not isinstance(goal, str) or len(goal) > 12000
            or (action == "run" and not goal.strip())):
        raise ValueError("invalid_request")
    if url is not None:
        if not isinstance(url, str) or len(url) > 4096:
            raise ValueError("invalid_request")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid_request")
    steps, timeout = request.get("max_steps", 20), request.get("timeout_seconds", 120)
    chrome_path = request.get("chrome_path")
    if chrome_path is not None and (not isinstance(chrome_path, str) or len(chrome_path) > 4096):
        raise ValueError("invalid_request")
    if type(steps) is not int or not 1 <= steps <= 30:
        raise ValueError("invalid_request")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 10 <= timeout <= 300:
        raise ValueError("invalid_request")
    tab_id, new_tab = request.get("tab_id"), request.get("new_tab", False)
    response, handoff = request.get("verification_response"), request.get("verification_handoff", "human")
    if (tab_id is not None and (not isinstance(tab_id, str) or not tab_id or len(tab_id) > 128)
            or type(new_tab) is not bool or (new_tab and (url is None or tab_id is not None))
            or handoff not in {"human", "agent"}
            or (response is not None and (not isinstance(response, str) or not response.strip()
                                         or len(response) > 500 or tab_id is None or url is not None))
            or (action == "tabs" and (url is not None or tab_id is not None or new_tab or response is not None))):
        raise ValueError("invalid_request")
    return {"action": action, "goal": goal.strip(), "start_url": url, "max_steps": steps,
            "timeout_seconds": timeout, "chrome_path": chrome_path, "tab_id": tab_id, "new_tab": new_tab,
            "verification_response": response, "verification_handoff": handoff}


def owned_tabs(chrome):
    return [{"id": target["targetId"], "title": str(target.get("title", ""))[:500],
             "url": str(target.get("url", ""))[:4096]}
            for target in chrome.call("Target.getTargets")["targetInfos"] if target["type"] == "page"][:64]


def read_message(stream):
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        raise EOFError()
    if len(line.encode("utf-8")) > MAX_LINE_BYTES or not line.endswith("\n"):
        raise ValueError("invalid_response")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("invalid_response")
    return message


class Protocol:
    def __init__(self, source, output):
        self.source, self.output = source, output
        self.messages = queue.Queue()
        self.stop = None
        self.finished = threading.Event()
        self.deadline = None
        self.next_id = 0
        self.approval_started = None
        self.approval_wait_seconds = 0.0
        self.approval_count = 0
        self.clock_lock = threading.Lock()
        self.secrets = [v for k, v in os.environ.items() if len(v) >= 8 and any(
            word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        )]

    def emit(self, event_type, **body):
        def redact(value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[redacted]")
                return value
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        encoded = json.dumps(
            redact({"type": event_type, "protocol_version": PROTOCOL_VERSION, **body}), ensure_ascii=False
        )
        self.output.write(encoded + "\n")
        self.output.flush()

    def check(self):
        if self.stop is not None:
            raise self.stop
        with self.clock_lock:
            now = time.monotonic()
            if self.approval_started is not None:
                if self.approval_wait_seconds + now - self.approval_started >= APPROVAL_ALLOWANCE_SECONDS:
                    raise Stopped("timeout", "approval_timeout")
            elif self.deadline is not None and now >= self.deadline:
                raise Stopped("timeout", "deadline_exceeded")

    def approve(self, kind, url, label, *, text=None, fingerprint=None):
        self.check()
        if any(secret in (url or "") or secret in (text or "") for secret in self.secrets):
            raise Stopped("denied", "credential_material")
        self.next_id += 1
        identifier = str(self.next_id)
        if not (kind == "navigate" and fingerprint == "initial"):
            self.approval_count += 1
        with self.clock_lock:
            self.approval_started = time.monotonic()
        try:
            self.check()
            self.emit("action_proposal", id=identifier, kind=kind, url=url, label=label,
                      text=text, fingerprint=fingerprint)
            while True:
                self.check()
                try:
                    response = self.messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.check()  # A late approval cannot authorize input after cancellation or timeout.
                if (response.get("type") != "action_response" or response.get("id") != identifier
                        or type(response.get("allow")) is not bool):
                    raise Stopped("error", "invalid_response")
                if not response["allow"]:
                    if response.get("stop_reason") == "approval_timeout":
                        raise Stopped("timeout", "approval_timeout")
                    raise Stopped("denied", "approval_denied")
                break
        finally:
            with self.clock_lock:
                waited = time.monotonic() - self.approval_started
                if self.deadline is not None:
                    self.deadline += waited
                self.approval_wait_seconds += waited
                self.approval_started = None
        # The watchdog can record cancellation while leaving this wait to unwind.
        # Recheck after that handoff so a just-approved mutation cannot escape it.
        self.check()

    def human_input(self, url):
        # One bounded handoff per task. No model calls while the human is responding.
        if self.deadline is not None:
            self.deadline += 180
        self.emit("human_required", url=url)
        while True:
            self.check()
            try:
                response = self.messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if response.get("type") != "human_response":
                raise Stopped("error", "invalid_response")
            answer = response.get("answer")
            if not isinstance(answer, str) or not answer.strip() or answer.lower() == "cancel":
                raise Stopped("blocked", "human_input_unavailable")
            return answer[:500]

    def read(self):
        while not self.finished.is_set():
            try:
                message = read_message(self.source)
            except EOFError:
                self.stop = Stopped("cancelled", "host_disconnected")
                return
            except (ValueError, OSError):
                self.stop = Stopped("error", "invalid_response")
                return
            if message.get("type") == "cancel":
                self.stop = Stopped("cancelled", "cancelled")
                return
            self.messages.put(message)

    def watch(self):
        # Interrupt even a blocking model request or approval wait on POSIX.
        while not self.finished.wait(0.05):
            try:
                self.check()
            except Stopped as stopped:
                self.stop = stopped
                with self.clock_lock:
                    waiting_for_approval = self.approval_started is not None
                # Approval reads poll every 100 ms; let their finally account for the wait.
                if not waiting_for_approval:
                    os.kill(os.getpid(), signal.SIGTERM)
                return


def usage_summary(state):
    result = {"decision_calls": len(state.get("decisions", [])), "text_calls": len(state.get("text_calls", [])),
              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_complete": True}
    for call in state.get("decisions", []) + state.get("text_calls", []):
        usage = call.get("usage", {})
        for target, keys in (("input_tokens", ("input_tokens", "prompt_tokens")),
                             ("output_tokens", ("output_tokens", "completion_tokens"))):
            value = next((usage[key] for key in keys if key in usage), 0)
            if type(value) in {int, float} and math.isfinite(value) and value >= 0:
                result[target] += value
        cost = usage.get("cost")
        if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
            result["cost_usd"] += cost
        else:
            result["cost_complete"] = False
    return result


def run_task(request, protocol, *, chrome_factory=OwnedChrome, browser_factory=Browser, agent_factory=Agent):
    started = time.monotonic()
    evidence, steps, seen = [], [], set()
    agent = None
    downloads = None
    tabs, current_tab_id, verification = [], None, None
    verification_url = None
    status, reason = "error", "worker_error"
    persistent = bool(os.environ.get("TINK_BROWSER_SESSION_DIR"))
    session_reused, browser_session = False, None
    rejected_decisions, repeated_rejections, rejection_key = 0, 0, None

    def observe(page):
        key = (page.get("url"), page.get("fingerprint"))
        if key not in seen:
            seen.add(key)
            item = {"url": str(page.get("url", ""))[:4096], "title": str(page.get("title", ""))[:500],
                    "text": str(page.get("text", ""))[:6000]}
            evidence.append(item)
            protocol.emit("observation", **item)

    def before_action(action, page, text):
        response = request.get("verification_response")
        if response and text and response in text and page["url"] != verification_url:
            raise Stopped("blocked", "verification_not_requested")
        if action.get("sensitive") and text:
            # Verify service credentials cannot be typed; mask only the login value in the proposal.
            if any(secret in text for secret in protocol.secrets):
                raise Stopped("denied", "credential_material")
            text = "[redacted password]"
        protocol.approve(action["kind"], page["url"], action["label"], text=text,
                         fingerprint=page["fingerprint"])

    try:
        if request["start_url"] is not None:
            protocol.approve("navigate", request["start_url"], "Open the requested starting page",
                             fingerprint="initial")
        elif not persistent:
            raise Stopped("blocked", "browser_session_missing")
        chrome_args = {"binary": request.get("chrome_path")}
        if request["start_url"] is None:
            chrome_args["allow_launch"] = False
        with chrome_factory(**chrome_args) as chrome, ExitStack() as resources:
            def followed_target(target):
                if persistent:
                    chrome.retain_target(target)
                chrome.call("Target.activateTarget", targetId=target)

            browser_args = {"transport": chrome.call, "on_target_changed": followed_target}
            if persistent:
                browser_session = chrome.session_data["browser_session"]
                tabs = owned_tabs(chrome)
                if request.get("action") == "tabs":
                    current_tab_id = chrome.session_data.get("target_id")
                    raise Stopped("completed", "tabs_listed")
                target = request.get("tab_id") or chrome.session_data.get("target_id")
                session_reused = any(t["id"] == target for t in tabs)
                if request.get("tab_id") and not session_reused:
                    raise Stopped("blocked", "browser_target_closed")
                if request.get("new_tab"):
                    session_reused = False
                if not session_reused:
                    if request["start_url"] is None:
                        raise Stopped("blocked", "browser_target_closed")
                    target = chrome.call("Target.createTarget", url="about:blank", background=True)["targetId"]
                if target != chrome.session_data.get("target_id"):
                    chrome.retain_target(target)
                browser_args.update(target=target, owns_target=False)
                pending = chrome.session_data.get("pending_verifications", {})
                pending = {tab: item for tab, item in pending.items()
                           if any(t["id"] == tab for t in tabs) and isinstance(item, dict)
                           and isinstance(item.get("url"), str)
                           and type(item.get("requested_at")) in {int, float}} if isinstance(pending, dict) else {}
                pending = dict(list(pending.items())[-64:])
                if request["start_url"] is not None:
                    pending.pop(target, None)
                chrome.session_data["pending_verifications"] = pending
                chrome._save_session()
            if os.environ.get("TINK_BROWSER_DOWNLOAD_DIR"):
                from .downloads import DownloadTracker

                downloads = resources.enter_context(DownloadTracker(
                    chrome, os.environ["TINK_BROWSER_DOWNLOAD_DIR"], check=protocol.check))
            browser = browser_factory(request["start_url"], **browser_args)
            try:
                # Activate only this Tink browser's owned target; never a personal Chrome tab.
                chrome.call("Target.activateTarget", targetId=browser.target)
                agent = agent_factory(request["start_url"], request["goal"], browser=browser,
                                      before_action=before_action)
                response = request.get("verification_response")
                pending = chrome.session_data.get("pending_verifications", {}) if persistent else {}
                challenge = pending.get(browser.target)
                # Reattaching changes the viewport/focus; an existing SPA form can
                # briefly disappear while it lays out. Keep the code out of model
                # context until that same pending challenge is observable again.
                if isinstance(challenge, dict):
                    render_deadline = time.monotonic() + 1.5
                    while (agent.state["page"]["url"] == challenge.get("url")
                           and not agent.state["page"].get("authenticationRequired")
                           and time.monotonic() < render_deadline):
                        protocol.check()
                        time.sleep(0.05)
                        agent.state["page"] = browser.observe(screenshot=False)
                matches = (isinstance(challenge, dict) and challenge.get("url") == agent.state["page"]["url"]
                           and agent.state["page"].get("authenticationRequired"))
                if challenge is not None and (response is not None or not matches):
                    pending.pop(browser.target, None)
                    chrome._save_session()
                if response is not None:
                    if not matches:
                        observe(agent.state["page"])
                        raise Stopped("blocked", "verification_not_requested")
                    agent.state["goal"] += "\nVerification response supplied for this tab: " + response
                    agent.sensitive_values.add(response)
                    verification_url = agent.state["page"]["url"]

                def require_verification(url=None):
                    nonlocal verification
                    verification = {"tab_id": browser.target, "url": url or agent.state["page"]["url"],
                                    "requested_at": time.time()}
                    if persistent:
                        pending.pop(browser.target, None)
                        while len(pending) >= 64:
                            pending.pop(next(iter(pending)))
                        pending[browser.target] = {key: verification[key] for key in ("url", "requested_at")}
                        chrome._save_session()
                    raise Stopped("blocked", "verification_required")

                protocol.emit("started", browser="owned_chrome", persistent_session=persistent,
                              session_reused=session_reused, browser_session=browser_session)
                observe(agent.state["page"])
                handoff_used = response is not None
                requesting_code = False
                while True:
                    protocol.check()
                    if response is not None and (agent.state["page"]["url"] != verification_url
                                                 or not agent.state["page"].get("authenticationRequired")):
                        agent.state["goal"] = request["goal"]
                        if agent.state["page"].get("authenticationRequired"):
                            raise Stopped("blocked", "verification_not_requested")
                        response = None
                    if agent.state["page"].get("authenticationRequired") and not handoff_used:
                        if request.get("verification_handoff") == "agent":
                            if not matches:
                                require_verification()
                            # A same-challenge follow-up gets one supervised decision to
                            # request a new code. Jev judges intent from the goal and
                            # observed control; no site labels or goal regex are assumed.
                            requesting_code = True
                            agent.state["goal"] = request["goal"] + "\n\n" + VERIFICATION_REQUEST
                        else:
                            handoff_used = True
                            answer = protocol.human_input(agent.state["page"]["url"])
                            agent.state["page"] = browser.observe(screenshot=False)
                            observe(agent.state["page"])
                            if answer.lower() != "continue":
                                agent.state["goal"] += "\nUser-provided verification response: " + answer
                            agent.state["status"] = "ready"
                    if len(agent.state["decisions"]) >= request["max_steps"] * 2:
                        raise Stopped("blocked", "decision_limit")
                    agent.command("predict")
                    # Permit DONE/BLOCKED after the last action, but never a further mutation.
                    if (len(agent.state["history"]) >= request["max_steps"]
                            and agent.state["decision"]["choice"] not in {"DONE", "BLOCKED"}):
                        raise Stopped("blocked", "step_limit")
                    selected = agent.state["decision"]["choice"]
                    if requesting_code:
                        page = agent.state["page"]
                        action = next((a for a in page["actions"] if a["id"] == selected), None)
                        if page["url"] != challenge["url"]:
                            pending.pop(browser.target, None)
                            chrome._save_session()
                            raise Stopped("blocked", "verification_not_requested")
                        if not page.get("authenticationRequired") or not action or action["kind"] != "click":
                            require_verification(challenge["url"])
                    previous_actions = len(agent.state["history"])
                    try:
                        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
                        repeated_rejections, rejection_key = 0, None
                    except StalePage:
                        agent.state["page"] = browser.observe(screenshot=False)
                        agent.state["status"] = "ready"
                        # A stale observation after executed input is not a rejected action.
                        if len(agent.state["history"]) == previous_actions:
                            rejected_decisions += 1
                            key = (agent.state["page"]["fingerprint"], selected)
                            repeated_rejections = repeated_rejections + 1 if key == rejection_key else 1
                            rejection_key = key
                            if repeated_rejections >= 3:
                                observe(agent.state["page"])
                                raise Stopped("blocked", "action_unavailable")
                        else:
                            repeated_rejections, rejection_key = 0, None
                    # An executed action stays recorded even if the following observation was stale.
                    for action in agent.state["history"][len(steps):]:
                        step = {"step": action["step"], "kind": action["kind"], "label": action["action"],
                                "url": action["url"], "elapsed_ms": action["elapsed_ms"]}
                        steps.append(step)
                        protocol.emit("step", **step)
                    observe(agent.state["page"])
                    if requesting_code:
                        # Even a stale action ends this attempt; never replay a resend
                        # or let the worker submit a form while the new code is absent.
                        if agent.state["page"]["url"] != challenge["url"]:
                            pending.pop(browser.target, None)
                            chrome._save_session()
                            raise Stopped("blocked", "verification_not_requested")
                        require_verification(challenge["url"])
                    if downloads is not None:
                        downloads.poll()
                    if agent.state["status"] == "done":
                        if downloads is not None:
                            downloads.wait()
                        status, reason = "completed", "model_done"
                        break
                    if agent.state["status"] == "blocked":
                        status, reason = "blocked", "model_blocked" if selected == "BLOCKED" else "no_progress"
                        break
            finally:
                # Do not let a second watchdog signal interrupt cleanup after the loop has stopped.
                protocol.finished.set()
                current_tab_id = browser.target
                if persistent:
                    try:
                        tabs = owned_tabs(chrome)
                    except Exception:
                        pass  # Preserve the task result if Chrome disappeared during cancellation.
                try:
                    browser.close()
                except Exception:
                    # A vanished CDP target must not replace cancellation or the recorded outcome.
                    # Persistent tasks detach; disposable tasks close their owned browser.
                    pass
    except Stopped as stopped:
        status, reason = stopped.status, stopped.reason
    except RuntimeError as error:
        if str(error) in {"browser_session_missing", "browser_profile_busy", "browser_target_closed",
                          "browser_session_unavailable", "multiple_new_tabs"}:
            status, reason = "blocked", str(error)
        elif str(error) in {"download_failed", "download_verification_failed", "download_cleanup_failed"}:
            status, reason = "error", str(error)
    except Exception:
        # Browser/provider exceptions may include URLs, page text, or credentials.
        status, reason = "error", "worker_error"
    state = agent.state if agent else {}
    for action in state.get("history", [])[len(steps):]:
        steps.append({"step": action["step"], "kind": action["kind"], "label": action["action"],
                      "url": action["url"], "elapsed_ms": action["elapsed_ms"]})
    usage = usage_summary(state)
    if status in {"error", "cancelled", "timeout"}:
        usage["cost_complete"] = False  # An interrupted/failed request may not have returned its usage.
    elapsed_ms = round((time.monotonic() - started) * 1000)
    approval_wait_ms = round(protocol.approval_wait_seconds * 1000)
    return {"status": status, "stop_reason": reason, "summary": SUMMARIES[reason], "evidence": evidence,
            "steps": steps, "step_count": len(steps), "usage": usage, "outcome_verified": False,
            "final_url": evidence[-1]["url"] if evidence else None,
            "elapsed_ms": elapsed_ms, "execution_ms": max(0, elapsed_ms - approval_wait_ms),
            "persistent_session": persistent, "session_reused": session_reused,
            "browser_session": browser_session, "rejected_decisions": rejected_decisions,
            "tabs": tabs, "current_tab_id": current_tab_id, "verification": verification,
            "downloads": downloads.files if downloads is not None else [],
            "approval_count": protocol.approval_count,
            "approval_wait_ms": approval_wait_ms}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--chrome-path")
    parser.add_argument("--close-browser", action="store_true")
    parser.add_argument("--cleanup-downloads", action="store_true")
    args = parser.parse_args()
    protocol = Protocol(sys.stdin, sys.stdout)
    if args.self_test:
        try:
            binary = chrome_binary(args.chrome_path)
        except RuntimeError:
            binary = None
        protocol.emit("self_test", worker="tink_browser", version="1", available=True, ready=True,
                      chrome_available=binary is not None, chrome_path=binary,
                      platform_supported=sys.platform != "win32", supervised_actions=True, persistent_session=True,
                      approval_wait_budget=True, popup_following=True, workflow_support=True)
        return
    if args.close_browser or args.cleanup_downloads:
        status, reason = "completed", "browser_closed" if args.close_browser else "downloads_cleaned"
        try:
            if os.environ.get("TINK_BROWSER_SESSION_DIR"):
                with OwnedChrome(binary=args.chrome_path, allow_launch=False) as chrome:
                    if args.close_browser:
                        chrome.shutdown()
        except RuntimeError as error:
            if str(error) != "browser_session_missing":
                status, reason = "blocked", str(error) if str(error) in SUMMARIES else "worker_error"
        except Exception:
            status, reason = "error", "worker_error"
        protocol.emit("result", status=status, stop_reason=reason, summary=SUMMARIES[reason],
                      persistent_session=True, session_reused=False, outcome_verified=False,
                      evidence=[], steps=[], step_count=0, usage=usage_summary({}), rejected_decisions=0)
        return
    try:
        request = validate_request(read_message(sys.stdin))
    except (ValueError, EOFError, OSError):
        protocol.emit("result", status="error", stop_reason="invalid_request", summary=SUMMARIES["invalid_request"],
                      evidence=[], steps=[], step_count=0, usage=usage_summary({}), outcome_verified=False,
                      final_url=None, elapsed_ms=0)
        return
    protocol.deadline = time.monotonic() + request["timeout_seconds"]

    def interrupted(_signum, _frame):
        if not protocol.finished.is_set():
            raise protocol.stop or Stopped("cancelled", "cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    threading.Thread(target=protocol.read, daemon=True).start()
    threading.Thread(target=protocol.watch, daemon=True).start()
    try:
        result = run_task(request, protocol)
    finally:
        protocol.finished.set()
    protocol.emit("result", **result)


if __name__ == "__main__":
    main()
