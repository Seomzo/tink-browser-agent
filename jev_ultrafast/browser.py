"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


def ensure_daemon():
    # The supervised worker supplies its own transport and must never load
    # another harness workspace's .env or create/reuse its daemon.
    from browser_harness.admin import ensure_daemon as ensure

    return ensure()


def cdp(*args, **kwargs):
    from browser_harness.helpers import cdp as call

    return call(*args, **kwargs)


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url, *, transport=None, target=None, owns_target=True, on_target_changed=None):
        if transport is None:
            ensure_daemon()
        self._cdp = transport or cdp
        self.owns_target = owns_target
        self.on_target_changed = on_target_changed
        self.pending_tabs = None
        self._attach(target or self._cdp("Target.createTarget", url="about:blank", background=True)["targetId"])
        if url is not None:
            self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def _attach(self, target):
        self.target = target
        self.session = self._cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def _follow_new_tab(self):
        pending, self.pending_tabs = self.pending_tabs, None
        if pending is None:
            return
        opener, previous, deadline = pending
        first_seen = None
        while True:
            candidates = [target for target in self._cdp("Target.getTargets")["targetInfos"]
                          if target["type"] == "page" and target.get("openerId") == opener
                          and target["targetId"] not in previous]
            if len(candidates) > 1:
                raise RuntimeError("multiple_new_tabs")
            now = time.monotonic()
            if candidates:
                first_seen = first_seen if first_seen is not None else now
                # Give sibling tabs from the same click a short chance to appear before choosing.
                if now - first_seen >= 0.1 or now >= deadline:
                    previous_session = self.session
                    self._attach(candidates[0]["targetId"])
                    self.on_target_changed(self.target)
                    self._cdp("Target.detachFromTarget", sessionId=previous_session)
                    self.after_input = None
                    return
            else:
                first_seen = None
            if now >= deadline:
                return
            time.sleep(0.02)

    def call(self, method, **params):
        return self._cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        # Agent records the executed click before this read-only target discovery.
        self._follow_new_tab()
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        empty_deadline = time.monotonic() + 2
        for attempt in range(110):
            try:
                page = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}, transport=self._cdp
                )
                if (not page["text"].strip() and not any(a["kind"] in {"click", "fill", "select"}
                        for a in page["actions"]) and time.monotonic() < empty_deadline):
                    time.sleep(0.02)
                    continue
                return page
            except StalePage:
                if attempt == 109:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def wait_for_change(self, page, timeout=1.5):
        """Give a pending SPA transition a bounded chance before accepting BLOCKED."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.observe(screenshot=False)
            if current["fingerprint"] != page["fingerprint"]:
                return current
            time.sleep(0.05)
        return page

    def fresh(self, page, action=None):
        if action is not None and action["kind"] == "back":
            if back_action(self.call("Page.getNavigationHistory")) != action:
                return False
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        previous = None
        if action["kind"] == "click" and self.on_target_changed is not None:
            previous = {target["targetId"] for target in self._cdp("Target.getTargets")["targetInfos"]}
        result = browser_operation(
            {"operation": "act", "session": self.session, "action": action, "text": text}, transport=self._cdp
        )
        self.after_input = action if action["kind"] != "wait" else None
        if previous is not None:
            self.pending_tabs = (self.target, previous, time.monotonic() + 1)
        return result

    def close(self):
        if self.target:
            if self.owns_target:
                self._cdp("Target.closeTarget", targetId=self.target)
            else:
                self._cdp("Target.detachFromTarget", sessionId=self.session)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def back_action(history):
    index, entries = history["currentIndex"], history["entries"]
    if index <= 0 or index >= len(entries):
        return None
    previous, current = entries[index - 1], entries[index]
    return {"id": "back", "kind": "back", "role": "navigation",
            "label": "Back to " + (previous.get("title") or previous["url"])[:200],
            "entry_id": previous["id"], "current_entry_id": current["id"],
            "url": previous["url"], "current_url": current["url"]}


def browser_operation(request, *, transport=None):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return (transport or cdp)(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind == "back":
            if back_action(call("Page.getNavigationHistory")) != action:
                raise StalePage("Navigation history changed. Observe again.")
            call("Page.navigateToHistoryEntry", entryId=action["entry_id"])
        elif kind == "key":
            if action.get("key") not in {"Enter", "Escape"} or type(action.get("node")) is not int:
                raise ValueError("Invalid observed keyboard action")
            allowed = evaluate("""(action => {
              const cache=window.__jevFast;
              return cache?.keyAllowed(cache.nodes.get(action.node),action.key) === true;
            })(""" + json.dumps(action) + ")")
            if allowed is not True:
                raise StalePage("Keyboard target changed. Observe again.")
            key = action["key"]
            params = {"key": key, "code": key, "windowsVirtualKeyCode": 13 if key == "Enter" else 27}
            call("Input.dispatchKeyEvent", type="keyDown", **params, **({"text": "\r"} if key == "Enter" else {}))
            call("Input.dispatchKeyEvent", type="keyUp", **params)
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const point=window.__jevFast?.hitPoint(e);
              if (!point) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return point;
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    previous = back_action(call("Page.getNavigationHistory"))
    if previous is not None:
        info["actions"].append(previous)
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
