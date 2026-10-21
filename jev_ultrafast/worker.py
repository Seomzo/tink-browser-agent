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
from urllib.parse import urlsplit

from .agent import Agent
from .browser import Browser, StalePage
from .worker_browser import OwnedChrome, chrome_binary

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 262144  # Accommodates JSON-escaped Unicode within the validated text/path limits.
SUMMARIES = {
    "human_input_unavailable": "Verification needs human input, but no response was received.",
    "model_done": "The browser model reported completion. Review the page evidence to verify the goal.",
    "model_blocked": "The browser model could not find a supported next action.",
    "no_progress": "The browser stopped after repeated actions without a visible change.",
    "step_limit": "The browser reached its action limit. Partial evidence is available.",
    "decision_limit": "The browser reached its decision limit. Partial evidence is available.",
    "approval_denied": "A proposed browser action was denied. No further action was executed.",
    "credential_material": "The worker refused to send a configured credential through a browser action.",
    "cancelled": "The browser task was cancelled.",
    "host_disconnected": "The supervising host disconnected.",
    "deadline_exceeded": "The browser task reached its time limit.",
    "invalid_request": "The browser request did not match protocol version 1.",
    "invalid_response": "The host returned an invalid action approval.",
    "worker_error": "The browser worker failed. Review partial evidence before retrying.",
}


class Stopped(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def validate_request(request):
    if not isinstance(request, dict) or request.get("type") != "start" or request.get("protocol_version") != 1:
        raise ValueError("invalid_request")
    goal, url = request.get("goal"), request.get("start_url")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
        raise ValueError("invalid_request")
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
    return {"goal": goal.strip(), "start_url": url, "max_steps": steps, "timeout_seconds": timeout,
            "chrome_path": chrome_path}


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
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise Stopped("timeout", "deadline_exceeded")

    def approve(self, kind, url, label, *, text=None, fingerprint=None):
        self.check()
        if any(secret in (url or "") or secret in (text or "") for secret in self.secrets):
            raise Stopped("denied", "credential_material")
        self.next_id += 1
        identifier = str(self.next_id)
        self.emit("action_proposal", id=identifier, kind=kind, url=url, label=label,
                  text=text, fingerprint=fingerprint)
        while True:
            self.check()
            try:
                response = self.messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if (response.get("type") != "action_response" or response.get("id") != identifier
                    or type(response.get("allow")) is not bool):
                raise Stopped("error", "invalid_response")
            if not response["allow"]:
                raise Stopped("denied", "approval_denied")
            return

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
    status, reason = "error", "worker_error"

    def observe(page):
        key = (page.get("url"), page.get("fingerprint"))
        if key not in seen:
            seen.add(key)
            item = {"url": str(page.get("url", ""))[:4096], "title": str(page.get("title", ""))[:500],
                    "text": str(page.get("text", ""))[:6000]}
            evidence.append(item)
            protocol.emit("observation", **item)

    def before_action(action, page, text):
        if action.get("sensitive") and text:
            # Verify service credentials cannot be typed; mask only the login value in the proposal.
            if any(secret in text for secret in protocol.secrets):
                raise Stopped("denied", "credential_material")
            text = "[redacted password]"
        protocol.approve(action["kind"], page["url"], action["label"], text=text,
                         fingerprint=page["fingerprint"])

    try:
        protocol.approve("navigate", request["start_url"], "Open the requested starting page", fingerprint="initial")
        with chrome_factory(binary=request.get("chrome_path")) as chrome:
            browser = browser_factory(request["start_url"], transport=chrome.call)
            try:
                # Activate only this disposable browser's owned target; never a personal Chrome tab.
                chrome.call("Target.activateTarget", targetId=browser.target)
                agent = agent_factory(request["start_url"], request["goal"], browser=browser,
                                      before_action=before_action)
                protocol.emit("started", browser="owned_chrome", persistent_session=False)
                observe(agent.state["page"])
                handoff_used = False
                while True:
                    protocol.check()
                    if agent.state["page"].get("authenticationRequired") and not handoff_used:
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
                    try:
                        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
                    except StalePage:
                        agent.state["page"] = browser.observe(screenshot=False)
                        agent.state["status"] = "ready"
                    # An executed action stays recorded even if the following observation was stale.
                    for action in agent.state["history"][len(steps):]:
                        step = {"step": action["step"], "kind": action["kind"], "label": action["action"],
                                "url": action["url"], "elapsed_ms": action["elapsed_ms"]}
                        steps.append(step)
                        protocol.emit("step", **step)
                    observe(agent.state["page"])
                    if agent.state["status"] == "done":
                        status, reason = "completed", "model_done"
                        break
                    if agent.state["status"] == "blocked":
                        status, reason = "blocked", "model_blocked" if selected == "BLOCKED" else "no_progress"
                        break
            finally:
                # Do not let a second watchdog signal interrupt cleanup after the loop has stopped.
                protocol.finished.set()
                try:
                    browser.close()
                except Exception:
                    # A vanished CDP target must not replace cancellation or the recorded outcome.
                    # OwnedChrome still terminates the entire disposable browser in its exit handler.
                    pass
    except Stopped as stopped:
        status, reason = stopped.status, stopped.reason
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
    return {"status": status, "stop_reason": reason, "summary": SUMMARIES[reason], "evidence": evidence,
            "steps": steps, "step_count": len(steps), "usage": usage, "outcome_verified": False,
            "final_url": evidence[-1]["url"] if evidence else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--chrome-path")
    args = parser.parse_args()
    protocol = Protocol(sys.stdin, sys.stdout)
    if args.self_test:
        try:
            binary = chrome_binary(args.chrome_path)
        except RuntimeError:
            binary = None
        protocol.emit("self_test", worker="tink_browser", version="1", available=True, ready=True,
                      chrome_available=binary is not None, chrome_path=binary,
                      platform_supported=sys.platform != "win32", supervised_actions=True)
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
