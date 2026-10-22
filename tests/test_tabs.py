"""Owned click-to-tab following without a browser or model endpoint."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as module
from jev_ultrafast.browser import Browser


def tab(identifier, opener='source'):
    return {'targetId': identifier, 'openerId': opener, 'type': 'page'}


@pytest.mark.parametrize('delay', [0, 0.35])
def test_only_new_direct_child_is_followed_and_old_session_detached(monkeypatch, delay):
    now = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    browser = Browser.__new__(Browser)
    browser.target, browser.session = 'source', 'source-session'
    browser.pending_tabs = ('source', {'source', 'old-child'}, 1.0)
    browser.on_target_changed = Mock()

    def respond(method, **params):
        if method == 'Target.getTargets':
            targets = [tab('old-child'), tab('unrelated', 'other'), tab('grandchild', 'new-child')]
            if now[0] >= delay:
                targets.append(tab('new-child'))
            return {'targetInfos': targets}
        if method == 'Target.attachToTarget':
            assert params['targetId'] == 'new-child'
            return {'sessionId': 'child-session'}
        return {}

    browser._cdp = Mock(side_effect=respond)
    browser._follow_new_tab()
    assert browser.target == 'new-child' and browser.session == 'child-session'
    browser.on_target_changed.assert_called_once_with('new-child')
    assert browser._cdp.call_args.args == ('Target.detachFromTarget',)
    assert browser._cdp.call_args.kwargs == {'sessionId': 'source-session'}
    assert browser.pending_tabs is None and now[0] < 0.5  # Immediate tabs do not pay the full discovery cap.


def test_multiple_matching_tabs_stop_without_selecting_or_replaying(monkeypatch):
    browser = Browser.__new__(Browser)
    browser.target, browser.session = 'source', 'source-session'
    browser.pending_tabs = ('source', {'source'}, 1.0)
    browser.on_target_changed = Mock()
    browser._cdp = Mock(return_value={'targetInfos': [tab('first'), tab('second')]})
    with pytest.raises(RuntimeError, match='multiple_new_tabs'):
        browser._follow_new_tab()
    assert browser.target == 'source' and browser.pending_tabs is None
    browser.on_target_changed.assert_not_called()
    assert browser._cdp.call_count == 1


def test_no_matching_popup_has_bounded_wait_and_never_attaches(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    browser = Browser.__new__(Browser)
    browser.pending_tabs = ('source', {'source', 'preexisting'}, 1.0)
    browser.on_target_changed = Mock()
    browser._cdp = Mock(return_value={'targetInfos': [tab('preexisting'), tab('unrelated', 'other')]})
    browser._follow_new_tab()
    assert 1 <= now[0] < 1.03
    assert all(call.args == ('Target.getTargets',) for call in browser._cdp.call_args_list)
    browser.on_target_changed.assert_not_called()


def test_click_snapshot_precedes_mutation_but_following_does_not(monkeypatch):
    browser = Browser.__new__(Browser)
    browser.target, browser.session = 'source', 'source-session'
    browser.fresh = Mock(return_value=True)
    browser.on_target_changed = Mock()
    browser._cdp = Mock(return_value={'targetInfos': [tab('source'), tab('preexisting')]})
    browser.pending_tabs = None

    def execute(_request, **_kwargs):
        browser._cdp.assert_called_once_with('Target.getTargets')
        browser.on_target_changed.assert_not_called()
        return {'executed': 'e1'}

    monkeypatch.setattr(module, 'browser_operation', execute)
    browser.act({'id': 'e1', 'kind': 'click'}, {})
    assert browser.pending_tabs[:2] == ('source', {'source', 'preexisting'})
    browser.on_target_changed.assert_not_called()
