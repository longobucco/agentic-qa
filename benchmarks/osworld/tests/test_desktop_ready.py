"""Pure regression tests for env.sandbox's desktop-rendering readiness gate -- no live sandbox.
Covers the "blank screen" race: Controller.ready() only checks that /screenshot returns
something without raising, never that it has real content, so a freshly started Xvfb buffer
with nothing painted onto it yet reports "ready" before Xvfb/openbox/D-Bus have actually
finished initializing (observed live on ~15% of runs across many unrelated tasks, confirmed on
tasks with AND without any config launch steps, e.g. task 937087b6 with config=[])."""
import io
from unittest.mock import MagicMock, patch

from PIL import Image

from benchmarks.osworld.env import sandbox


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _blank_screenshot():
    return _png_bytes(Image.new("RGB", (100, 100), color=(10, 10, 10)))  # one flat color


def _rendered_screenshot():
    # enough color variety to look like a real desktop -- a simple gradient/noise pattern is
    # sufficient and deterministic, no need for anything fancy
    img = Image.new("RGB", (100, 100))
    px = img.load()
    for x in range(100):
        for y in range(100):
            px[x, y] = (x * 2 % 256, y * 2 % 256, (x + y) % 256)
    return _png_bytes(img)


def test_desktop_rendered_false_for_blank_screenshot():
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _blank_screenshot()
    assert sandbox._desktop_rendered(ctrl) is False


def test_desktop_rendered_true_for_screenshot_with_color_variety():
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _rendered_screenshot()
    assert sandbox._desktop_rendered(ctrl) is True


def test_desktop_rendered_false_not_raises_when_screenshot_raises():
    ctrl = MagicMock()
    ctrl.screenshot.side_effect = Exception("connection reset")
    assert sandbox._desktop_rendered(ctrl) is False


def test_wait_for_desktop_ready_returns_none_once_rendered_after_a_few_polls():
    ctrl = MagicMock()
    ctrl.screenshot.side_effect = [
        _blank_screenshot(),
        _blank_screenshot(),
        _rendered_screenshot(),
        _rendered_screenshot(),  # confirms the first rendered read wasn't a one-off flicker
    ]
    with patch("time.sleep") as mock_sleep:
        result = sandbox._wait_for_desktop_ready(ctrl, timeout=30, poll=2)
    assert result is None
    assert ctrl.screenshot.call_count == 4
    # slept between every pair of reads, not zero times (i.e. this didn't just happen to pass
    # on the very first screenshot)
    assert mock_sleep.call_count == 3


def test_wait_for_desktop_ready_requires_sustained_render_not_a_single_flicker():
    # Reproduces the exact race this gate exists to catch (confirmed live 2026-09-21): the
    # desktop renders real content on the very first read, then relapses to blank before a
    # second read confirms it -- a single-shot check would have declared this ready.
    ctrl = MagicMock()
    ctrl.screenshot.side_effect = [
        _rendered_screenshot(),  # looks ready...
        _blank_screenshot(),     # ...but relapses before the confirming read
        _rendered_screenshot(),
        _rendered_screenshot(),  # now genuinely stable across two consecutive reads
    ]
    with patch("time.sleep"):
        result = sandbox._wait_for_desktop_ready(ctrl, timeout=30, poll=2)
    assert result is None
    assert ctrl.screenshot.call_count == 4


def test_wait_for_desktop_ready_returns_error_string_after_timeout_exhausted():
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _blank_screenshot()
    with patch("time.sleep"):
        result = sandbox._wait_for_desktop_ready(ctrl, timeout=0.01, poll=0.001)
    assert result is not None
    assert isinstance(result, str)
    assert result != ""
    assert "never rendered" in result or "timeout" in result.lower()


def test_run_config_calls_readiness_gate_before_config_steps_and_short_circuits():
    ctrl = MagicMock()
    task = {"id": "test-task", "config": []}
    with patch.object(sandbox, "_wait_for_desktop_ready",
                       return_value="desktop never rendered real content within 30s") as mock_gate:
        result = sandbox._run_config(ctrl, task)
    mock_gate.assert_called_once_with(ctrl)
    assert result == "desktop never rendered real content within 30s"
