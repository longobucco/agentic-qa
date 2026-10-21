import io
import json
from unittest.mock import MagicMock

import pytest
from PIL import Image

from benchmarks.osworld import config
from benchmarks.osworld.mcp import official_computer as oc

GOLDEN = {
    '{"action": "left_click", "coordinate": [100, 200]}': "pyautogui.click(100, 200)\n",
    '{"action": "left_click", "coordinate": [100, 200], "text": "ctrl"}':
        "pyautogui.keyDown('ctrl')\npyautogui.click(100, 200)\npyautogui.keyUp('ctrl')\n",
    '{"action": "triple_click", "coordinate": [5, 6]}': "pyautogui.tripleClick(5, 6)\n",
    '{"action": "right_click"}': "pyautogui.rightClick()\n",
    '{"action": "key", "text": "ctrl+Page_Down", "repeat": 2}':
        "pyautogui.keyDown('ctrl')\npyautogui.keyDown('pagedown')\npyautogui.keyUp('pagedown')\n"
        "pyautogui.keyUp('ctrl')\npyautogui.keyDown('ctrl')\npyautogui.keyDown('pagedown')\n"
        "pyautogui.keyUp('pagedown')\npyautogui.keyUp('ctrl')\n",
    '{"action": "scroll", "coordinate": [10, 20], "scroll_direction": "down", "scroll_amount": 3}':
        "pyautogui.scroll(-3, 10, 20)\n",
    '{"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]}':
        "pyautogui.moveTo(1, 2, duration=0.5)\npyautogui.dragTo(3, 4, duration=0.5)\n",
    '{"action": "wait"}': "pyautogui.sleep(0.5)\n",
    '{"action": "fail"}': "FAIL",
    '{"action": "done"}': "DONE",
}
W, H = config.SCREEN_WIDTH, config.SCREEN_HEIGHT


@pytest.mark.parametrize("inp,expected", GOLDEN.items())
def test_translation_matches_upstream(inp, expected):
    assert oc.to_pyautogui(json.loads(inp)) == expected


def test_type_presses_each_character_like_upstream():
    code = oc.to_pyautogui({"action": "type", "text": "a\nb"})
    assert code == "pyautogui.press('a')\npyautogui.press('enter')\npyautogui.press('b')\n"


def test_invalid_action_raises():
    with pytest.raises(ValueError):
        oc.to_pyautogui({"action": "teleport"})


def _png():
    b = io.BytesIO()
    Image.new("RGB", (W, H), (9, 9, 9)).save(b, format="PNG")
    return b.getvalue()


def _session(max_steps=3):
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _png()
    slept = []
    return oc.ComputerSession(ctrl, max_steps=max_steps, sleep=slept.append), ctrl, slept


def test_action_executes_with_upstream_prefix_pauses_and_returns_screenshot():
    s, ctrl, slept = _session()
    out = s.call({"action": "left_click", "coordinate": [1, 2]})
    cmd = ctrl.execute.call_args[0][0]
    assert cmd[:2] == ["python", "-c"]
    assert cmd[2] == oc.PKGS_PREFIX.format(command="pyautogui.click(1, 2)\n")
    assert ctrl.execute.call_args.kwargs.get("retry_timeouts") is False
    assert slept == [0.5]
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "text"]
    assert out[0][1] == "Success" and out[2][1] == "[Current step: 1/3]"


def test_first_screenshot_is_free_later_ones_count():
    s, _, _ = _session()
    s.call({"action": "screenshot"})
    assert s.steps_used == 0
    s.call({"action": "screenshot"})
    assert s.steps_used == 1


def test_first_non_screenshot_call_makes_a_later_screenshot_cost_a_step():
    s, _, _ = _session()
    s.call({"action": "left_click", "coordinate": [1, 2]})
    assert s.steps_used == 1
    s.call({"action": "screenshot"})
    assert s.steps_used == 2


def test_single_screenshot_action_executes_and_sleeps():
    s, ctrl, slept = _session()
    s.call({"action": "left_click", "coordinate": [1, 2]})  # burns the free-first-screenshot slot
    ctrl.execute.reset_mock()
    slept.clear()
    s.call({"action": "screenshot"})
    assert ctrl.execute.call_count == 1
    cmd = ctrl.execute.call_args[0][0]
    assert cmd[2] == oc.PKGS_PREFIX.format(command="pyautogui.sleep(0.1)\n")
    assert slept == [0.5]


def test_budget_exhausted_executes_nothing():
    s, ctrl, _ = _session(max_steps=1)
    s.call({"action": "left_click", "coordinate": [1, 2]})
    ctrl.execute.reset_mock()
    out = s.call({"action": "left_click", "coordinate": [3, 4]})
    ctrl.execute.assert_not_called()
    assert out == [("text", "Step limit reached (1/1). No further actions are executed; stop now.")]


def test_batch_stops_before_executing_if_any_sub_action_fails_to_translate():
    # Ruling (fix round 1): batches translate every sub-action FIRST; any translation error
    # aborts the whole batch with nothing executed at all (not "stop after the first success").
    s, ctrl, _ = _session()
    out = s.call({"actions": [{"action": "key", "text": "a"}, {"action": "teleport"},
                              {"action": "key", "text": "b"}]})
    assert s.steps_used == 1
    assert ctrl.execute.call_count == 0
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "text"]
    assert "teleport" in out[0][1]


def test_valid_batch_executes_one_combined_command_and_sleeps_once():
    s, ctrl, slept = _session()
    out = s.call({"actions": [{"action": "key", "text": "a"}, {"action": "key", "text": "b"}]})
    assert s.steps_used == 1
    assert ctrl.execute.call_count == 1
    code_a = oc.to_pyautogui({"action": "key", "text": "a"})
    code_b = oc.to_pyautogui({"action": "key", "text": "b"})
    cmd = ctrl.execute.call_args[0][0]
    assert cmd[2] == oc.PKGS_PREFIX.format(command=code_a + code_b)
    assert slept == [0.5]
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "text"]


def test_batch_zoom_not_last_returns_zoom_then_full_screenshot():
    s, ctrl, _ = _session()
    out = s.call({"actions": [{"action": "zoom", "region": [0, 0, W // 4, H // 4]},
                              {"action": "key", "text": "a"}]})
    assert ctrl.execute.call_count == 1  # one combined command, zoom's own code included
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "image", "text"]
    images = [v for k, v in out if k == "image"]
    assert Image.open(io.BytesIO(images[0])).size == (W, H)  # zoom, fitted to the screen
    assert Image.open(io.BytesIO(images[1])).size == (W, H)  # full screenshot, native size


def test_batch_zoom_last_returns_only_the_zoom_image():
    s, _, _ = _session()
    out = s.call({"actions": [{"action": "key", "text": "a"},
                              {"action": "zoom", "region": [0, 0, W // 4, H // 4]}]})
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "text"]


def test_zoom_returns_the_region_scaled_to_the_screen_instead_of_the_screenshot():
    s, _, _ = _session()
    out = s.call({"action": "zoom", "region": [0, 0, W // 4, H // 4]})
    img = next(v for k, v in out if k == "image")
    assert Image.open(io.BytesIO(img)).size == (W, H)


def test_done_and_fail_actions_execute_nothing():
    s, ctrl, _ = _session()
    s.call({"action": "fail"})
    ctrl.execute.assert_not_called()


def test_single_invalid_action_returns_three_item_shape_with_screenshot():
    s, ctrl, _ = _session()
    out = s.call({"action": "teleport"})
    assert ctrl.execute.call_count == 0
    kinds = [k for k, _ in out]
    assert kinds == ["text", "image", "text"]
    assert "teleport" in out[0][1]


def test_execute_timeout_is_not_retried_and_reports_error():
    s, ctrl, _ = _session()
    ctrl.execute.side_effect = TimeoutError("read timed out")
    out = s.call({"action": "left_click", "coordinate": [1, 2]})
    assert ctrl.execute.call_count == 1
    assert ctrl.execute.call_args.kwargs.get("retry_timeouts") is False
    assert any(k == "text" and "TimeoutError" in v for k, v in out)


def test_screenshot_failure_degrades_to_text_only_instead_of_raising():
    s, ctrl, _ = _session()
    ctrl.screenshot.side_effect = OSError("guest unreachable")
    out = s.call({"action": "left_click", "coordinate": [1, 2]})
    assert out == [("text", "Success"), ("text", "[Current step: 1/3]")]


def test_missing_action_raises_value_error():
    with pytest.raises(ValueError):
        oc.to_pyautogui({})
