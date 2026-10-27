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


def test_duplicate_link_labels_include_bounded_card_context_and_actual_destination(browser):
    browser.evaluate("""document.body.innerHTML=`
      <div><h2>2-Day GA</h2><div><a href="https://tickets.example.test/ga">Select Tickets</a></div></div>
      <div><h2>2-Day GA+</h2><div><a href="https://tickets.example.test/ga-plus">Select Tickets</a></div></div>
      <p>${'Unrelated content '.repeat(100)}</p>`""")
    page = browser.observe(screenshot=False)
    links = [a for a in page["actions"] if a["label"] == "Select Tickets"]
    assert [(a["context"], a["href"]) for a in links] == [
        ("2-Day GA Select Tickets", "https://tickets.example.test/ga"),
        ("2-Day GA+ Select Tickets", "https://tickets.example.test/ga-plus"),
    ]
    assert all(len(a.get("context", "")) <= 400 for a in page["actions"])
    browser.evaluate("document.querySelector('h2').textContent='Changed ticket tier'")
    assert not browser.fresh(page, links[0])


@pytest.mark.parametrize("nested", [True, False])
def test_card_link_uses_uncovered_point_without_clicking_a_nested_control(browser, nested):
    cover = '<button id="save" onclick="event.stopPropagation();window.saved=true">Save</button>'
    browser.evaluate("""document.body.innerHTML=`
      <style>
        #card { display:block;position:relative;width:400px;height:220px; }
        #save { position:absolute;left:140px;top:80px;width:120px;height:60px;margin:0; }
      </style>
      <div style="position:relative">
        <a id="card" href="https://homes.example.test/house" aria-label="Home in Los Angeles"
           onclick="event.preventDefault();window.opened=true">Listing image
           """ + (cover if nested else "") + """
        </a>""" + ("" if nested else cover) + """
      </div>`""")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["label"] == "Home in Los Angeles")
    rect = action["rect"]
    assert browser.evaluate(f"document.elementFromPoint({rect['x'] + rect['w'] / 2},"
                            f"{rect['y'] + rect['h'] / 2}).id") == "save"
    browser.act(action, page)
    assert browser.evaluate("window.opened === true && window.saved !== true")


def test_partly_visible_large_link_can_be_clicked_within_its_visible_region(browser):
    browser.evaluate("""document.body.innerHTML=`
      <a href="https://homes.example.test/house" aria-label="Large listing"
         style="display:block;position:fixed;top:-900px;width:300px;height:1100px"
         onclick="event.preventDefault();window.opened=true">Large listing</a>`""")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["label"] == "Large listing")
    browser.act(action, page)
    assert browser.evaluate("window.opened === true")


@pytest.mark.parametrize("kind", ["native", "dialog", "menu"])
def test_observed_escape_dismisses_visible_dom_overlay_once(browser, kind):
    if kind == "native":
        browser.evaluate("""document.body.innerHTML='<dialog><button>Inside dialog</button></dialog>';
          document.querySelector('dialog').addEventListener('cancel',()=>window.dismissals=(window.dismissals||0)+1);
          document.querySelector('dialog').showModal()""")
        closed = "!document.querySelector('dialog').open"
    else:
        browser.evaluate(f"""document.body.innerHTML='<div role="{kind}" tabindex="-1">Open overlay</div>';
          const overlay=document.querySelector('[role]'); overlay.focus();
          overlay.onkeydown=event=>{{if(event.key==='Escape'){{overlay.hidden=true;
            window.dismissals=(window.dismissals||0)+1;}}}}""")
        closed = "document.querySelector('[role]').hidden"
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("key") == "Escape")
    browser.act(action, page)
    page = browser.observe(screenshot=False)
    assert browser.evaluate(closed)
    assert browser.evaluate("window.dismissals") == 1
    assert not any(a.get("key") == "Escape" for a in page["actions"])


def test_focused_search_enter_submits_once_and_focus_change_rejects_stale_action(browser):
    browser.evaluate("""document.body.innerHTML=`<form><input type="search" aria-label="Search"></form>
      <input aria-label="Other"><p id="result"></p>`;
      document.querySelector('form').onsubmit=event=>{event.preventDefault();
        window.submits=(window.submits||0)+1;
        document.querySelector('#result').textContent=document.querySelector('input').value};
      document.querySelector('input').value='observed search'; document.querySelector('input').focus()""")
    page = browser.observe(screenshot=False)
    enter = next(a for a in page["actions"] if a.get("key") == "Enter")
    browser.evaluate("document.querySelectorAll('input')[1].focus()")
    with pytest.raises(StalePage):
        browser.act(enter, page)
    assert browser.evaluate("window.submits||0") == 0
    browser.evaluate("document.querySelector('input').focus()")
    page = browser.observe(screenshot=False)
    enter = next(a for a in page["actions"] if a.get("key") == "Enter")
    browser.act(enter, page)
    browser.observe(screenshot=False)
    assert browser.evaluate("document.querySelector('#result').textContent") == "observed search"
    assert browser.evaluate("window.submits") == 1


@pytest.mark.parametrize("change", ["field", "overlay"])
def test_keyboard_rechecks_dom_after_approval(browser, change):
    from jev_ultrafast.browser import browser_operation

    if change == "field":
        browser.evaluate("document.querySelector('input').focus()")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a.get("key") == "Enter")
        browser.evaluate("document.querySelector('input').readOnly=true")
    else:
        browser.evaluate("""document.body.innerHTML='<div role="menu" tabindex="-1">Menu</div>';
          document.querySelector('[role]').focus()""")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a.get("key") == "Escape")
        browser.evaluate("document.querySelector('[role]').hidden=true")
    # Exercise the final executor guard even when a state change follows fresh().
    with pytest.raises(StalePage):
        browser_operation({"operation": "act", "session": browser.session, "action": action},
                          transport=browser._cdp)


def test_back_uses_observed_history_and_rejects_same_url_new_entry(browser):
    browser.evaluate("history.pushState({},'', '#one');history.pushState({},'', '#two')")
    page = browser.observe(screenshot=False)
    back = next(a for a in page["actions"] if a["kind"] == "back")
    browser.evaluate("history.pushState({},'', '#two')")
    with pytest.raises(StalePage):
        browser.act(back, page)
    page = browser.observe(screenshot=False)
    back = next(a for a in page["actions"] if a["kind"] == "back")
    before = browser.call("Page.getNavigationHistory")["currentIndex"]
    browser.act(back, page)
    browser.observe(screenshot=False)
    assert browser.call("Page.getNavigationHistory")["currentIndex"] == before - 1
    page = browser.observe(screenshot=False)
    browser.act(next(a for a in page["actions"] if a["kind"] == "back"), page)
    browser.observe(screenshot=False)
    assert browser.evaluate("location.hash") == "#one"
