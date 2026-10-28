"""Opt-in DOM checks using temporary Chrome only; no external sites or model calls."""

import json
import os
import sys
import time
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from jev_ultrafast import agent as loop
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


COUNTDOWN_FORM = """
<form id="verification" action="https://example.test/verify" onsubmit="event.preventDefault()">
  <h2 id="heading">Verify sign-in</h2>
  <p id="account">Account: example account; total $10</p>
  <button id="total" disabled>Total $10</button>
  <label><span id="label">Code</span><input id="code" aria-describedby="description"
    oninput="window.fills=(window.fills||0)+1"></label>
  <p id="description">Use the current code</p>
  <button id="verify" type="button" onclick="window.verifications=(window.verifications||0)+1">Verify</button>
  <button id="resend" type="button" role="timer" disabled>Resend in 30s</button>
</form>"""


def test_countdown_during_prediction_text_generation_and_approval_executes_each_action_once(browser, monkeypatch):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    browser.evaluate("window.ticks=0; window.timer=setInterval(()=>{ "
                     "document.querySelector('#resend').textContent='Resend in '+(++window.ticks)+'s'; },100)")

    def predict(page, *_args, **_kwargs):
        action = next(a for a in page["actions"] if a["kind"] == "fill")
        time.sleep(0.15)
        return {"choice": action["id"], "probabilities": {action["id"]: 1}, "confidence": 1,
                "latency_ms": 0, "operation": "TYPE_TEXT", "target": "1", "usage": {}}

    def generate_text(_context):
        time.sleep(0.15)
        return "test-code", {"model": "offline", "latency_ms": 0}

    helper = Mock(side_effect=generate_text)
    approval = Mock(side_effect=lambda *_args: time.sleep(1.05))
    monkeypatch.setattr(loop, "choose", predict)
    monkeypatch.setattr(loop, "field_text", helper)
    agent = loop.Agent(None, "Fill the test code", browser=browser, before_action=approval)
    result = agent.command("tick")
    assert len(result["history"]) == 1 and result["history"][0]["kind"] == "fill"
    assert browser.evaluate("document.querySelector('#code').value") == "test-code"
    assert browser.evaluate("window.fills") == 1
    assert browser.evaluate("window.ticks") >= 10
    helper.assert_called_once()
    approval.assert_called_once()
    page = browser.observe(screenshot=False)
    verify = next(a for a in page["actions"] if a["label"] == "Verify")
    time.sleep(0.15)
    assert not browser.fresh(page)  # The full observation still includes the live text.
    browser.act(verify, page)
    assert browser.evaluate("window.verifications") == 1
    browser.evaluate("clearInterval(window.timer)")


@pytest.mark.parametrize("markup", [
    '<span id="resend" role="timer">30 seconds</span>',
    '<button id="resend" aria-disabled="true" role="timer">30 seconds</button>',
])
def test_unrelated_semantic_timer_text_does_not_invalidate_target(browser, markup):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    browser.evaluate("document.querySelector('#resend').outerHTML=" + json.dumps(markup))
    page = browser.observe(screenshot=False)
    browser.evaluate("document.querySelector('#resend').textContent='29 seconds'")
    for action in page["actions"]:
        if action["kind"] == "fill" or action["label"] == "Verify":
            assert browser.fresh(page, action)


@pytest.mark.parametrize("change", [
    "document.querySelector('#code').value='already changed'",
    "document.querySelector('#code').outerHTML=document.querySelector('#code').outerHTML",
    "document.querySelector('#label').textContent='Different credential'",
    "document.querySelector('#account').textContent='Account: another account; total $10'",
    "document.querySelector('#account').textContent='Account: example account; total $100'",
    "document.querySelector('#total').textContent='Total $100'",
    "document.querySelector('#heading').textContent='Confirm purchase'",
    "document.querySelector('#verification').action='https://example.test/different'",
    "const old=document.querySelector('#verification'), replacement=document.createElement('form');"
    "replacement.action=old.action; replacement.append(...old.childNodes); old.replaceWith(replacement)",
    "document.querySelector('#description').setAttribute('role','timer');"
    "document.querySelector('#description').textContent='Use another credential'",
    "document.querySelector('#account').setAttribute('aria-live','polite');"
    "document.querySelector('#account').textContent='Account: example account; total $100'",
    "document.querySelector('#verify').textContent='Confirm purchase'",
    "document.querySelector('#resend').outerHTML='<span>Unmarked changed text 29</span>'",
])
@pytest.mark.parametrize("kind", ["fill", "click"])
def test_changed_target_form_description_and_static_context_still_rejects(browser, change, kind):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == kind and
                  (kind == "fill" or a["label"] == "Verify"))
    browser.evaluate(change)
    with pytest.raises(StalePage):
        browser.act(action, page, text="must-not-type")
    assert browser.evaluate("window.fills || 0") == 0
    assert browser.evaluate("window.verifications || 0") == 0


def test_selected_countdown_control_own_label_change_still_rejects_click(browser):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    browser.evaluate("document.querySelector('#resend').disabled=false")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["label"] == "Resend in 30s")
    browser.evaluate("document.querySelector('#resend').textContent='Resend in 29s'")
    with pytest.raises(StalePage):
        browser.act(action, page)


@pytest.mark.parametrize("kind", ["fill", "click"])
def test_associated_field_description_stays_guarded_even_when_marked_as_timer(browser, kind):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    browser.evaluate("document.querySelector('#description').setAttribute('role','timer')")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == kind and
                  (kind == "fill" or a["label"] == "Verify"))
    browser.evaluate("document.querySelector('#description').textContent='Code for another account'")
    with pytest.raises(StalePage):
        browser.act(action, page, text="must-not-type")
    assert browser.evaluate("window.fills || 0") == 0
    assert browser.evaluate("window.verifications || 0") == 0


def test_non_content_nodes_never_pollute_control_labels_or_freshness(browser):
    browser.evaluate("""document.body.innerHTML=`<button onclick="window.clicks=(window.clicks||0)+1">
      <span>RO</span><style id="generated-style">#generated-123{overflow:hidden}</style><span>Repair Order</span>
      <script type="application/json">{"hidden":"script content"}</script>
      <noscript>fallback content</noscript><template>template content</template></button>`""")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    assert action["label"].split() == ["RO", "Repair", "Order"]
    assert "generated-123" not in page["text"]
    browser.evaluate("document.querySelector('#generated-style').textContent='#generated-456{overflow:hidden}'")
    assert browser.fresh(page, action)
    browser.act(action, page)
    assert browser.evaluate("window.clicks") == 1


@pytest.mark.parametrize("kind", ["fill", "click"])
def test_unmarked_disabled_countdown_stays_conservatively_guarded(browser, kind):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COUNTDOWN_FORM))
    browser.evaluate("document.querySelector('#resend').removeAttribute('role')")
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == kind and
                  (kind == "fill" or a["label"] == "Verify"))
    browser.evaluate("document.querySelector('#resend').textContent='Resend in 29s'")
    with pytest.raises(StalePage):
        browser.act(action, page, text="must-not-type")
    assert browser.evaluate("window.fills || 0") == 0
    assert browser.evaluate("window.verifications || 0") == 0


ICON_HTML = """<style>.icon{display:inline-block;width:40px;height:40px;cursor:pointer}</style>
<span id="badge"><div id="icon" class="icon" role="img" aria-label="Filter records"
 onclick="window.filters=(window.filters||0)+1;document.querySelector('#panel').hidden=false">
 <svg width="40" height="40"><rect width="40" height="40" fill="blue"/></svg>
</div></span><div id="panel" hidden>Filter panel</div>
<div id="cover" style="position:fixed;inset:0;z-index:100;background:white" hidden></div>"""


def test_labeled_pointer_icon_is_an_observed_click_target(browser):
    browser.evaluate("document.body.innerHTML=" + json.dumps(ICON_HTML))
    page = browser.observe(screenshot=False)
    icons = [a for a in page["actions"] if a.get("role") == "img"]
    assert len(icons) == 1
    assert (icons[0]["kind"], icons[0]["label"]) == ("click", "Filter records")
    browser.act(icons[0], page)
    assert browser.evaluate("window.filters") == 1
    assert browser.evaluate("document.querySelector('#panel').hidden") is False


@pytest.mark.parametrize("change", [
    "document.querySelector('#icon').style.cursor='default'",
    "document.querySelector('#icon').removeAttribute('aria-label')",
    "document.querySelector('#icon').style.display='none'",
    "document.querySelector('#cover').hidden=false",
    "document.querySelector('#icon').setAttribute('aria-disabled','true')",
    "document.querySelector('#badge').setAttribute('aria-disabled','true')",
    "document.querySelector('#badge').inert=true",
])
def test_non_actionable_or_decorative_icons_are_not_exposed(browser, change):
    browser.evaluate("document.body.innerHTML=" + json.dumps(ICON_HTML))
    browser.evaluate(change)
    page = browser.observe(screenshot=False)
    assert not any(a.get("role") == "img" for a in page["actions"])


def test_icon_inside_existing_button_is_not_duplicated_or_blocking_button_hit(browser):
    browser.evaluate("""document.body.innerHTML=`
      <button aria-label="Open menu" onclick="window.menus=(window.menus||0)+1">
        <span role="img" aria-label="Menu icon" style="display:block;width:100%;height:100%;cursor:pointer">
          <svg width="200" height="50"><rect width="200" height="50" fill="blue"/></svg>
        </span>
      </button>`""")
    page = browser.observe(screenshot=False)
    clicks = [a for a in page["actions"] if a["kind"] == "click"]
    assert [a["label"] for a in clicks] == ["Open menu"]
    browser.act(clicks[0], page)
    assert browser.evaluate("window.menus") == 1


@pytest.mark.parametrize("change", [
    "document.querySelector('#icon').style.cursor='default'",
    "document.querySelector('#cover').hidden=false",
    "document.querySelector('#badge').setAttribute('aria-disabled','true')",
    "const button=document.createElement('button'); document.querySelector('#badge').append(button);"
    "button.append(document.querySelector('#icon'))",
])
@pytest.mark.parametrize("direct_executor", [False, True])
def test_icon_eligibility_is_rechecked_before_any_input(browser, change, direct_executor):
    from jev_ultrafast.browser import browser_operation

    browser.evaluate("document.body.innerHTML=" + json.dumps(ICON_HTML))
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("role") == "img")
    browser.evaluate(change)
    assert not browser.fresh(page, action)
    with pytest.raises(StalePage):
        if direct_executor:
            # Also cover a change between the freshness read and the final input check.
            browser_operation({"operation": "act", "session": browser.session, "action": action},
                              transport=browser._cdp)
        else:
            browser.act(action, page)
    assert browser.evaluate("window.filters || 0") == 0


COMBOBOX_HTML = """<style>
#value-container{position:relative;display:grid;align-items:center;width:240px;height:40px}
#input-container{grid-area:1/1;width:2.46px;height:24px}
#combo{box-sizing:border-box;width:2.46px;height:24px;margin:0;padding:0;border:0;opacity:1}
#selected-value{grid-area:1/1;z-index:2;width:220px;height:30px;background:white}
</style><div id="group"><div id="value-container" onmousedown="
 if(document.querySelector('#combo').getAttribute('aria-expanded')==='false'){
   window.opens=(window.opens||0)+1;
   document.querySelector('#combo').setAttribute('aria-expanded','true');
   document.querySelector('#selected-value').style.display='none';
   document.querySelector('#combo').style.width='220px';
   document.querySelector('#input-container').style.width='220px';
   document.querySelector('#choices').hidden=false;
 }">
 <div id="selected-value">Black Flags</div><div id="input-container">
  <input id="combo" role="combobox" aria-haspopup="true" aria-expanded="false"
   oninput="window.fills=(window.fills||0)+1">
 </div></div></div>
<div id="choices" role="listbox" hidden><div role="option" onclick="window.chosen='Closed'">Closed</div></div>
<div id="cover" style="position:fixed;inset:0;z-index:100;background:white" hidden></div>"""


def test_composite_combobox_opens_observed_value_without_filling_covered_input(browser):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COMBOBOX_HTML))
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("open_node"))
    assert (action["kind"], action["label"], action["value"]) == ("click", "Open Black Flags", "Black Flags")
    assert not any(a["kind"] == "fill" for a in page["actions"])
    assert browser.evaluate("window.__jevFast.hitPoint(document.querySelector('#combo'))") is None
    assert browser.evaluate(f"window.__jevFast.nodes.get({action['open_node']}).id") == "value-container"
    assert browser.evaluate(f"window.__jevFast.nodes.get({action['open_display']}).id") == "selected-value"
    browser.act(action, page)
    assert browser.evaluate("window.opens") == 1
    assert browser.evaluate("window.fills || 0") == 0
    opened = browser.observe(screenshot=False)
    assert any(a["kind"] == "fill" and a["node"] == action["node"] for a in opened["actions"])
    option = next(a for a in opened["actions"] if a["label"] == "Closed")
    browser.act(option, opened)
    assert browser.evaluate("window.chosen") == "Closed"


@pytest.mark.parametrize("change", [
    "document.querySelector('#value-container').append(document.createElement('input'))",
    "const other=document.createElement('input');other.setAttribute('role','combobox');"
    "document.querySelector('#value-container').append(other)",
    "document.querySelector('#value-container').insertAdjacentHTML('beforeend','<button>Clear</button>')",
    "document.querySelector('#value-container').insertAdjacentHTML('beforeend',"
    "'<span role=img aria-label=Clear style=cursor:pointer>Clear</span>')",
    "document.querySelector('#cover').hidden=false",
    "document.querySelector('#combo').readOnly=true",
    "document.querySelector('#combo').style.opacity='0'",
    "document.querySelector('#group').setAttribute('aria-disabled','true')",
    "document.querySelector('#value-container').insertAdjacentHTML('beforeend','<div>Other value</div>')",
])
def test_ambiguous_or_non_actionable_composite_combobox_is_not_exposed(browser, change):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COMBOBOX_HTML))
    browser.evaluate(change)
    page = browser.observe(screenshot=False)
    assert not any(a.get("open_node") for a in page["actions"])


@pytest.mark.parametrize("change", [
    "document.querySelector('#cover').hidden=false",
    "document.querySelector('#combo').outerHTML=document.querySelector('#combo').outerHTML",
    "const old=document.querySelector('#value-container'), replacement=old.cloneNode(false);"
    "replacement.append(...old.childNodes); old.replaceWith(replacement)",
    "document.querySelector('#selected-value').outerHTML=document.querySelector('#selected-value').outerHTML",
    "document.querySelector('#value-container').append(document.createElement('input'))",
    "document.querySelector('#value-container').insertAdjacentHTML('beforeend','<button>Clear</button>')",
    "document.querySelector('#combo').readOnly=true",
    "document.querySelector('#group').setAttribute('aria-disabled','true')",
])
@pytest.mark.parametrize("direct_executor", [False, True])
def test_composite_combobox_binding_is_rechecked_before_input(browser, change, direct_executor):
    from jev_ultrafast.browser import browser_operation

    browser.evaluate("document.body.innerHTML=" + json.dumps(COMBOBOX_HTML))
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("open_node"))
    browser.evaluate(change)
    assert not browser.fresh(page, action)
    with pytest.raises(StalePage):
        if direct_executor:
            browser_operation({"operation": "act", "session": browser.session, "action": action},
                              transport=browser._cdp)
        else:
            browser.act(action, page)
    assert browser.evaluate("window.opens || 0") == 0
    assert browser.evaluate("window.fills || 0") == 0


def test_composite_combobox_selected_value_change_invalidates_decision(browser):
    browser.evaluate("document.body.innerHTML=" + json.dumps(COMBOBOX_HTML))
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("open_node"))
    browser.evaluate("document.querySelector('#selected-value').textContent='Different filter'")
    with pytest.raises(StalePage):
        browser.act(action, page)
    assert browser.evaluate("window.opens || 0") == 0


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


def keyboard_observation(browser, key):
    browser.evaluate("""document.body.innerHTML=`
      <aside><div id="overlay" role="menu" tabindex="-1">Open menu</div></aside>
      <form><input id="query" type="search" aria-label="Search" value="observed search"></form>
      <input id="other" aria-label="Other field"><p id="ticker">30 seconds</p>`;
      window.keyExecutions=0;
      document.querySelector('#overlay').onkeydown=event=>{
        if(event.key==='Escape'){window.keyExecutions++;event.currentTarget.hidden=true;}
      };
      document.querySelector('form').onsubmit=event=>{event.preventDefault();window.keyExecutions++;}
    """)
    browser.evaluate("window.keyboardTarget=document.querySelector(" +
                     json.dumps("#overlay" if key == "Escape" else "#query") + ");window.keyboardTarget.focus()")
    page = browser.observe(screenshot=False)
    return page, next(a for a in page["actions"] if a.get("key") == key)


@pytest.mark.parametrize("key", ["Escape", "Enter"])
def test_keyboard_target_remains_fresh_after_unrelated_ticker_changes(browser, key):
    page, action = keyboard_observation(browser, key)
    browser.evaluate("document.querySelector('#ticker').textContent='29 seconds'")
    assert not browser.fresh(page)  # The whole-page observation changed.
    assert browser.fresh(page, action)
    browser.act(action, page)
    assert browser.evaluate("window.keyExecutions") == 1
    assert browser.evaluate("document.querySelector('#query').value") == "observed search"


@pytest.mark.parametrize("key", ["Escape", "Enter"])
@pytest.mark.parametrize("change", [
    "document.querySelector('#other').value='changed field value'",
    "history.pushState({},'', '#changed-document-url')",
    "window.keyboardTarget.outerHTML=window.keyboardTarget.outerHTML",
    "window.keyboardTarget.setAttribute('aria-label','Changed purpose')",
    "document.body.insertAdjacentHTML('beforeend','<div id=new-menu role=menu tabindex=-1>Other menu</div>');"
    "document.querySelector('#new-menu').focus()",
])
def test_targeted_keyboard_freshness_preserves_document_field_target_and_overlay_guards(browser, key, change):
    page, action = keyboard_observation(browser, key)
    browser.evaluate(change)
    assert not browser.fresh(page, action)
    with pytest.raises(StalePage):
        browser.act(action, page)
    assert browser.evaluate("window.keyExecutions") == 0


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
