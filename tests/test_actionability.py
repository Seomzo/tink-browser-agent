"""Opt-in DOM checks using temporary Chrome only; no external sites or model calls."""

import os
import sys
from urllib.parse import quote

import pytest

from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.worker_browser import OwnedChrome

pytestmark = pytest.mark.skipif(
    os.environ.get("TINK_TEST_OWNED_CHROME") != "1" or sys.platform == "win32",
    reason="Opt in to disposable Chrome tests with TINK_TEST_OWNED_CHROME=1 on macOS/Linux",
)

HTML = """<!doctype html><title>Actionability fixture</title>
<style>
body { margin: 20px; }
button, input, select { display: block; width: 200px; height: 50px; margin: 10px; }
#child { display: block; height: 100%; }
#cover { position: fixed; inset: 0; z-index: 100; background: white; }
</style>
<button id="target" onclick="window.clicks=(window.clicks||0)+1"><span id="child">Continue</span></button>
<input aria-label="City">
<select aria-label="Category"><option>All</option><option>Design</option></select>
<div id="cover" hidden>
  <button onclick="document.querySelector('#cover').hidden=true">Dismiss overlay</button>
</div>"""


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setenv("TINK_BROWSER_HEADLESS", "1")
    monkeypatch.delenv("TINK_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.delenv("TINK_BROWSER_SESSION_DIR", raising=False)
    with OwnedChrome() as chrome:
        browser = Browser("data:text/html," + quote(HTML), transport=chrome.call)
        try:
            yield browser
        finally:
            browser.close()


def test_covered_controls_are_omitted_and_reappear_after_dismissal(browser):
    browser.evaluate("document.querySelector('#cover').hidden=false")
    page = browser.observe(screenshot=False)
    controls = [a for a in page["actions"] if a["kind"] in {"click", "fill", "select"}]
    assert [(a["kind"], a["label"]) for a in controls] == [("click", "Dismiss overlay")]

    browser.act(controls[0], page)
    page = browser.observe(screenshot=False)
    assert {"Continue", "City", "Open City", "Category → Design"} <= {a["label"] for a in page["actions"]}
    assert not any(a["label"] == "Dismiss overlay" for a in page["actions"])


def test_descendant_hit_and_pointer_transparent_overlay_allow_execution(browser):
    browser.evaluate("document.querySelector('#cover').hidden=false; "
                     "document.querySelector('#cover').style.pointerEvents='none'")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["label"] == "Continue")
    rect = action["rect"]
    assert browser.evaluate(f"document.elementFromPoint({rect['x'] + rect['w'] / 2},"
                            f"{rect['y'] + rect['h'] / 2}).id") == "child"
    browser.act(action, page)
    assert browser.evaluate("window.clicks") == 1


def test_overlay_appearing_after_observation_prevents_execution(browser):
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["label"] == "Continue")
    browser.evaluate("document.querySelector('#cover').hidden=false")
    assert not browser.fresh(page)
    with pytest.raises(StalePage):
        browser.act(action, page)
    assert browser.evaluate("window.clicks || 0") == 0
