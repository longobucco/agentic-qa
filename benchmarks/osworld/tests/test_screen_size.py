"""Screen size must be one value everywhere: the guest's Xvfb, the SetupController's placeholder
substitution, and what runners assume. A mismatch is an environment error, never a silent
off-target click."""
import io
from unittest.mock import MagicMock, patch

from PIL import Image

from benchmarks.osworld import config
from benchmarks.osworld.env import osworld_eval, sandbox


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_defaults_are_1080p():
    assert (config.SCREEN_WIDTH, config.SCREEN_HEIGHT) == (1920, 1080)


def test_setup_controller_gets_the_configured_screen_size():
    sc = osworld_eval.make_setup_controller("http://127.0.0.1:5000")
    assert (sc.screen_width, sc.screen_height) == (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)


def test_matching_screenshot_is_not_a_mismatch():
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _png(config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
    assert sandbox._screen_size_mismatch(ctrl) is None


def test_old_1280x1024_image_is_reported():
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _png(1280, 1024)
    err = sandbox._screen_size_mismatch(ctrl)
    assert err and "1280x1024" in err and "1920x1080" in err


def test_unreadable_screenshot_is_reported_not_raised():
    ctrl = MagicMock()
    ctrl.screenshot.side_effect = RuntimeError("guest down")
    assert "could not read" in sandbox._screen_size_mismatch(ctrl)


def test_run_config_stops_on_a_size_mismatch_before_any_config_step():
    ctrl = MagicMock()
    task = {"id": "t", "config": [{"type": "launch", "parameters": {"command": ["x"]}}]}
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value="size mismatch"), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller") as msc:
        assert sandbox._run_config(ctrl, task) == "size mismatch"
    msc.assert_not_called()
