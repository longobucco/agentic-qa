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


def test_budget_exhausted_executes_nothing():
    s, ctrl, _ = _session(max_steps=1)
    s.call({"action": "left_click", "coordinate": [1, 2]})
    ctrl.execute.reset_mock()
    out = s.call({"action": "left_click", "coordinate": [3, 4]})
    ctrl.execute.assert_not_called()
    assert out == [("text", "Step limit reached (1/1). No further actions are executed; stop now.")]


def test_batch_is_one_step_and_stops_at_first_error():
    s, ctrl, _ = _session()
    out = s.call({"actions": [{"action": "key", "text": "a"}, {"action": "teleport"},
                              {"action": "key", "text": "b"}]})
    assert s.steps_used == 1
    assert ctrl.execute.call_count == 1
    assert any(k == "text" and "teleport" in v for k, v in out)


def test_zoom_returns_the_region_scaled_to_the_screen_instead_of_the_screenshot():
    s, _, _ = _session()
    out = s.call({"action": "zoom", "region": [0, 0, W // 4, H // 4]})
    img = next(v for k, v in out if k == "image")
    assert Image.open(io.BytesIO(img)).size == (W, H)


def test_done_and_fail_actions_execute_nothing():
    s, ctrl, _ = _session()
    s.call({"action": "fail"})
    ctrl.execute.assert_not_called()
