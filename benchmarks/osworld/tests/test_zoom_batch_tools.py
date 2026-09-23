import importlib
import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from benchmarks.osworld import config, prompts
from benchmarks.osworld.mcp import zoom_batch_tools as zb
from core import codex_loop

W, H = config.SCREEN_WIDTH, config.SCREEN_HEIGHT
OK = json.dumps({"status": "success", "output": ""})


def _ctrl():
    c = MagicMock()
    buf = io.BytesIO()
    Image.new("RGB", (W, H), (1, 2, 3)).save(buf, format="PNG")
    c.screenshot.return_value = buf.getvalue()
    c.pyautogui.return_value = OK
    return c


def test_zoom_scales_the_region_up_to_fit_the_screen():
    png = zb.zoom_png(_ctrl(), 0, 0, W // 4, H // 4)
    assert Image.open(io.BytesIO(png)).size == (W, H)


@pytest.mark.parametrize("region", [(100, 100, 50, 50), (0, 0, W + 1, 10), (-1, 0, 10, 10)])
def test_bad_zoom_region_raises(region):
    with pytest.raises(ValueError):
        zb.zoom_png(_ctrl(), *region)


def test_batch_runs_in_order_and_reports_each_action():
    c = _ctrl()
    out = zb.run_batch(c, [{"action": "click", "x": 10, "y": 20},
                           {"action": "type", "text": "a\nb"},
                           {"action": "key", "keys": "ctrl+s"}])
    codes = [call.args[0] for call in c.pyautogui.call_args_list]
    assert "pyautogui.click(10, 20)" in codes[0]
    compile(codes[1], "<t>", "exec")
    assert "hotkey" in codes[2]
    assert out.splitlines() == ["1. ok: click", "2. ok: type", "3. ok: key"]


def test_batch_stops_at_the_first_failure():
    c = _ctrl()
    out = zb.run_batch(c, [{"action": "click", "x": 1, "y": 1},
                           {"action": "click", "x": W + 5, "y": 1},
                           {"action": "type", "text": "x"}]).splitlines()
    assert out[0] == "1. ok: click"
    assert out[1].startswith("2. error:") and "outside the screen" in out[1]
    assert out[2] == f"3. {zb.NOT_EXECUTED}"
    assert c.pyautogui.call_count == 1


def test_guest_side_error_counts_as_a_failure():
    c = _ctrl()
    c.pyautogui.side_effect = [json.dumps({"status": "error", "message": "boom"}), OK]
    out = zb.run_batch(c, [{"action": "key", "keys": "f5"}, {"action": "key", "keys": "f6"}])
    assert out.splitlines()[0].startswith("1. error:") and "boom" in out
    assert c.pyautogui.call_count == 1


def test_batch_limits():
    c = _ctrl()
    assert zb.run_batch(c, []).startswith("error:")
    too_many = [{"action": "key", "keys": "a"}] * (zb.MAX_BATCH + 1)
    assert "nothing executed" in zb.run_batch(c, too_many)
    assert c.pyautogui.call_count == 0
    assert "unsupported" in zb.run_batch(c, [{"action": "teleport"}])


def test_batch_wait_sleeps_host_side_capped_at_30s():
    slept = []
    zb.run_batch(_ctrl(), [{"action": "wait", "seconds": 99}], sleep=slept.append)
    assert slept == [30]


def test_codex_allowed_tools_follow_the_flag():
    assert codex_loop.allowed_mcp_tools(False) == codex_loop.ALLOWED_MCP_TOOLS
    assert codex_loop.allowed_mcp_tools(True) == codex_loop.ALLOWED_MCP_TOOLS + ("zoom", "batch")


def test_prompt_advertises_the_tools_iff_the_flag_is_on(monkeypatch):
    task = {"instruction": "x"}
    monkeypatch.setattr(prompts, "ZOOM_BATCH", False)
    assert "zoom(" not in prompts.agent_prompt(task)
    monkeypatch.setattr(prompts, "ZOOM_BATCH", True)
    text = prompts.agent_prompt(task)
    assert "zoom(x0, y0, x1, y1)" in text and "batch(actions)" in text


def test_flag_gives_both_arms_their_own_tree():
    env = {"OSW_ZOOM_BATCH": "1", "OSW_SYSTEM_SUFFIX": "", "OSW_ASTRA_SYSTEM_SUFFIX": "",
           "OSW_EFFORT": "", "OSW_GROUNDING": "0"}
    with patch.dict(os.environ, env):
        c = importlib.reload(config)
        names = (c.SYSTEM_NAME, c.ASTRA_SYSTEM_NAME)
    importlib.reload(config)
    assert all(n.endswith("_zoombatch") for n in names)


def test_flag_reaches_both_mcp_children(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_BATCH", True)
    from benchmarks.osworld.runners import common
    path = common._mcp_config("http://c")
    try:
        env = json.loads(open(path).read())["mcpServers"]["osworld"]["env"]
    finally:
        os.unlink(path)
    assert env["OSW_ZOOM_BATCH"] == "1"
    cmd = codex_loop.build_codex_cmd("p", model="m", cwd="/tmp", controller_url="http://c",
                                     zoom_batch=True)
    assert 'OSW_ZOOM_BATCH = "1"' in " ".join(cmd)
