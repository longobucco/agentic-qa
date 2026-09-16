"""Pure regression tests for env.sandbox's _verify_launches -- no live sandbox. Covers the
window-mapped check added on top of the pre-existing process-alive (pgrep) check: under a cold
container start, a process can be alive for seconds before painting anything, and the agent's
first screenshot can be handed over blank (observed: closed-book tasks
215dfd39/a5bbbcd5/28cc3b7e run 1). A process-only check would have missed this."""
from unittest.mock import MagicMock, patch

from benchmarks.osworld.env import sandbox

STEPS = [{"type": "launch", "parameters": {"command": "vlc --no-audio"}}]


def _fake_ctrl(side_effect):
    ctrl = MagicMock()
    ctrl.execute.side_effect = side_effect
    return ctrl


def test_process_running_and_window_mapped_succeeds():
    # wmctrl output deliberately uppercased ("VLC-Window-Title") against a lowercase binary
    # name ("vlc") so this test actually exercises the `.lower()` calls in _window_mapped --
    # it must fail if either `.lower()` were removed from the implementation.
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return "0x00000001  0 VLC-Window-Title"
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is None


def test_process_running_but_window_never_mapped_fails():
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return "0x00000001  0 some-other-app"
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is not None
    assert "never started/rendered" in result
    assert "vlc" in result


def test_wmctrl_error_falls_back_to_true_and_does_not_block_success():
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return None
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    assert sandbox._window_mapped(ctrl, "vlc") is True
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is None
