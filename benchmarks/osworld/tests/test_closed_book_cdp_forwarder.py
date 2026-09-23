"""Regression tests for the 2026-09-23 split of `use_proxy` into two independent flags
(`use_proxy` and `enable_cdp_forwarder`) in env.sandbox._run_config and
env.osworld_eval.evaluate_official/_score.

Before this split, CDP routing (chrome_open_tabs/chrome_close_tabs support) and the
open-book-specific --proxy-server=127.0.0.1:18888 injection were both gated on the same
`use_proxy` boolean, so extending CDP routing to closed-book would also have wrongly injected
a --proxy-server flag pointing at nothing (only open-book's guest fixture proxy listens there).
These tests pin the two behaviors apart: enabling the CDP forwarder must NOT also turn on the
--proxy-server injection unless `use_proxy` is separately True."""
import tempfile
from unittest.mock import MagicMock, patch

from benchmarks.osworld.env import sandbox, osworld_eval


def _fake_ctrl():
    return MagicMock(base_url="http://fake-controller")


# --- env.sandbox._run_config -------------------------------------------------------------------

def test_enable_cdp_forwarder_alone_does_not_turn_on_use_proxy():
    """The core of the split: enable_cdp_forwarder=True, use_proxy=False (the closed-book call
    shape) must start a CdpForwarder AND call SetupController.setup with use_proxy=False."""
    ctrl = _fake_ctrl()
    task = {"id": "t1", "config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]}
    fake_setup_ctrl = MagicMock()
    fake_cdp_fwd = MagicMock(host="127.0.0.1", port=12345)

    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.CdpForwarder") as MockForwarder, \
         patch("benchmarks.osworld.env.cdp_forwarder.inject_remote_allow_origins",
               side_effect=lambda steps: steps) as mock_inject, \
         patch.object(sandbox, "_verify_launches", return_value=None):
        MockForwarder.return_value.start.return_value = fake_cdp_fwd
        sandbox._run_config(ctrl, task, use_proxy=False, enable_cdp_forwarder=True)

    assert MockForwarder.called, "CdpForwarder should have been constructed"
    mock_inject.assert_called_once()
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False, (
        "use_proxy must stay False -- injecting --proxy-server against a closed-book image "
        "with no proxy listening would break every Chrome launch")


def test_neither_flag_skips_the_cdp_forwarder_entirely():
    ctrl = _fake_ctrl()
    task = {"id": "t2", "config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]}
    fake_setup_ctrl = MagicMock()

    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.CdpForwarder") as MockForwarder, \
         patch.object(sandbox, "_verify_launches", return_value=None):
        sandbox._run_config(ctrl, task, use_proxy=False, enable_cdp_forwarder=False)

    assert not MockForwarder.called, "no flag set -- CdpForwarder must not be touched at all"
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False


def test_use_proxy_true_still_implies_the_forwarder_unchanged_from_before_the_split():
    """Open-book's existing call shape (use_proxy=True, enable_cdp_forwarder defaulted False)
    must keep working exactly as before -- the `or` in the gate covers it."""
    ctrl = _fake_ctrl()
    task = {"id": "t3", "config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]}
    fake_setup_ctrl = MagicMock()
    fake_cdp_fwd = MagicMock(host="127.0.0.1", port=12345)

    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.CdpForwarder") as MockForwarder, \
         patch("benchmarks.osworld.env.cdp_forwarder.inject_remote_allow_origins",
               side_effect=lambda steps: steps), \
         patch.object(sandbox, "_verify_launches", return_value=None):
        MockForwarder.return_value.start.return_value = fake_cdp_fwd
        sandbox._run_config(ctrl, task, use_proxy=True, enable_cdp_forwarder=False)

    assert MockForwarder.called
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is True


# --- env.osworld_eval.evaluate_official / _score -------------------------------------------------

def _task_with_postconfig():
    return {
        "id": "t-eval",
        "evaluator": {
            "func": "infeasible",
            "postconfig": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}],
        },
    }


def test_score_postconfig_cdp_forwarder_independent_of_use_proxy():
    fake_setup_ctrl = MagicMock()
    fake_cdp_fwd = MagicMock(host="127.0.0.1", port=54321)

    with patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.inject_remote_allow_origins",
               side_effect=lambda steps: steps) as mock_inject:
        # func == "infeasible" short-circuits scoring itself, but postconfig setup still runs
        # first -- exercising exactly the code path under test without needing real getters.
        env = MagicMock(action_history=[])
        ev = _task_with_postconfig()["evaluator"]
        osworld_eval._score(env, ev, "infeasible", "http://fake-controller", tempfile.mkdtemp(),
                            getters=None, metrics=None, use_proxy=False, cdp_forwarder=fake_cdp_fwd)

    mock_inject.assert_called_once()
    assert fake_setup_ctrl.vm_ip == "127.0.0.1"
    assert fake_setup_ctrl.chromium_port == 54321
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False, (
        "cdp_forwarder being set must not force use_proxy=True onto the postconfig setup call")


def test_score_no_forwarder_leaves_postconfig_untouched():
    fake_setup_ctrl = MagicMock()

    with patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.inject_remote_allow_origins") as mock_inject:
        env = MagicMock(action_history=[])
        ev = _task_with_postconfig()["evaluator"]
        osworld_eval._score(env, ev, "infeasible", "http://fake-controller", tempfile.mkdtemp(),
                            getters=None, metrics=None, use_proxy=False, cdp_forwarder=None)

    assert not mock_inject.called
