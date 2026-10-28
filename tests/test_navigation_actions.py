"""Only observed code-owned keyboard/history targets can reach browser input."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast.browser import back_action, browser_operation
from jev_ultrafast.model import action_space


def history():
    return {"currentIndex": 1, "entries": [
        {"id": 7, "url": "https://example.test/start", "title": "Start"},
        {"id": 8, "url": "https://example.test/detail", "title": "Detail"},
    ]}


def test_synthetic_targets_share_click_operation_without_overwriting_field_click():
    actions = [
        {"id": "field", "kind": "fill", "node": 1, "label": "Search", "role": "searchbox"},
        {"id": "focus", "kind": "click", "node": 1, "label": "Open Search"},
        {"id": "submit", "kind": "key", "node": 1, "key": "Enter", "label": "Press Enter in Search"},
        back_action(history()),
    ]
    elements, targets, controls = action_space(actions)
    assert set(targets) == {"CLICK", "TYPE_TEXT"}
    assert [action["id"] for action in targets["CLICK"].values()] == ["focus", "submit", "back"]
    assert len(elements) == 3 and controls == {}
    assert targets["TYPE_TEXT"]["1"]["id"] == "field"


@pytest.mark.parametrize("key", ["Tab", "Delete", "a", "Control+L", "Enter; arbitrary code"])
def test_executor_rejects_non_whitelisted_keys_before_transport(key):
    transport = Mock()
    with pytest.raises(ValueError, match="Invalid observed keyboard"):
        browser_operation({"operation": "act", "session": "owned", "action": {
            "id": "key", "kind": "key", "node": 1, "key": key,
        }}, transport=transport)
    transport.assert_not_called()


@pytest.mark.parametrize("key,code", [("Enter", 13), ("Escape", 27)])
def test_keyboard_is_revalidated_then_dispatched_once(key, code):
    transport = Mock(return_value={"result": {"value": True}})
    action = {"id": "key", "kind": "key", "node": 1, "key": key}
    assert browser_operation({"operation": "act", "session": "owned", "action": action},
                             transport=transport) == {"executed": "key"}
    assert [call.args[0] for call in transport.call_args_list] == [
        "Runtime.evaluate", "Input.dispatchKeyEvent", "Input.dispatchKeyEvent",
    ]
    down, up = transport.call_args_list[1:]
    assert down.kwargs["key"] == key and down.kwargs["windowsVirtualKeyCode"] == code
    assert down.kwargs["type"] == "keyDown" and up.kwargs["type"] == "keyUp"


def test_changed_keyboard_target_never_dispatches():
    transport = Mock(return_value={"result": {"value": False}})
    with pytest.raises(ValueError, match="Keyboard target changed"):
        browser_operation({"operation": "act", "session": "owned", "action": {
            "id": "key", "kind": "key", "node": 1, "key": "Enter",
        }}, transport=transport)
    assert transport.call_count == 1


def test_back_is_bound_to_observed_current_and_previous_entries():
    observed = history()
    action = back_action(observed)
    transport = Mock(return_value=observed)
    assert browser_operation({"operation": "act", "session": "owned", "action": action},
                             transport=transport) == {"executed": "back"}
    assert transport.call_args.args == ("Page.navigateToHistoryEntry",)
    assert transport.call_args.kwargs["entryId"] == 7
    changed = deepcopy(observed)
    changed["entries"][1]["id"] = 9
    transport.reset_mock()
    transport.return_value = changed
    with pytest.raises(ValueError, match="Navigation history changed"):
        browser_operation({"operation": "act", "session": "owned", "action": action}, transport=transport)
    assert transport.call_count == 1
    assert back_action({"currentIndex": 0, "entries": observed["entries"]}) is None
