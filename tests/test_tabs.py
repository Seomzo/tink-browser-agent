"""Owned click-to-tab following without a browser or model endpoint."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as module
from jev_ultrafast.browser import Browser, StalePage


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
    browser._follow_new_tab(settle=True)
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


def test_no_popup_observation_is_nonblocking_and_barrier_waits_only_remaining_time(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    browser = Browser.__new__(Browser)
    browser.pending_tabs = ('source', {'source', 'preexisting'}, 1.0)
    browser.on_target_changed = Mock()
    browser._cdp = Mock(return_value={'targetInfos': [tab('preexisting'), tab('unrelated', 'other')]})
    browser._follow_new_tab()
    assert now[0] == 0 and browser._cdp.call_count == 1
    assert browser.pending_tabs is not None
    now[0] = 0.5  # Model/approval work overlapped half of the discovery window.
    browser._follow_new_tab(settle=True)
    assert 1 <= now[0] < 1.03
    assert browser.pending_tabs is None
    assert all(call.args == ('Target.getTargets',) for call in browser._cdp.call_args_list)
    browser.on_target_changed.assert_not_called()


@pytest.mark.parametrize('work_elapsed', [0, 0.5, 1.2])
def test_delayed_child_invalidates_old_tab_action_after_overlapped_work(monkeypatch, work_elapsed):
    now = [work_elapsed]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ))
    browser = Browser.__new__(Browser)
    browser.target, browser.session = 'source', 'source-session'
    browser.pending_tabs = ('source', {'source'}, 1.0)
    browser.on_target_changed = Mock()

    def respond(method, **_params):
        if method == 'Target.getTargets':
            return {'targetInfos': [tab('new-child')] if now[0] >= 0.3 else []}
        return {'sessionId': 'child-session'} if method == 'Target.attachToTarget' else {}

    browser._cdp = Mock(side_effect=respond)
    execute = Mock()
    monkeypatch.setattr(module, 'browser_operation', execute)
    with pytest.raises(StalePage):
        browser.act({'id': 'old-action', 'kind': 'click', 'node': 1}, {})
    assert browser.target == 'new-child'
    execute.assert_not_called()
    browser.on_target_changed.assert_called_once_with('new-child')


@pytest.mark.parametrize('selected', ['DONE', 'BLOCKED'])
def test_terminal_agent_decision_explicitly_settles_pending_tabs(selected):
    import time

    from jev_ultrafast.agent import Agent

    agent = Agent.__new__(Agent)
    browser = Mock(fresh=Mock(return_value=True))
    page = {'fingerprint': 'observed'}
    agent.blocked_refresh_used = True
    agent.state = {'page': page, 'decision': {'choice': selected}, 'browser': browser,
                   'started_at': time.perf_counter(), 'status': 'predicted'}
    agent.snapshot = Mock(return_value={})
    agent.command('act', {'fingerprint': 'observed'})
    browser.fresh.assert_called_once_with(page, settle_tabs=True)


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
