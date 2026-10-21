"""Regression tests for `enable_cdp_forwarder` in env.sandbox._run_config and
env.osworld_eval.evaluate_official/_score -- the CDP-routing flag that makes
chrome_open_tabs/chrome_close_tabs steps work, independent of the (now-removed) open-book-only
--proxy-server injection. These tests pin: enabling the CDP forwarder must not implicitly turn on
--proxy-server (the closed-book image has nothing listening there)."""
import tempfile
from unittest.mock import MagicMock, patch

from benchmarks.osworld.env import sandbox, osworld_eval


def _fake_ctrl():
    return MagicMock(base_url="http://fake-controller")


# --- env.sandbox._run_config -------------------------------------------------------------------

def test_enable_cdp_forwarder_starts_the_forwarder_without_a_proxy_server():
    """enable_cdp_forwarder=True (the closed-book call shape) must start a CdpForwarder AND call
    SetupController.setup with use_proxy=False."""
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
        sandbox._run_config(ctrl, task, enable_cdp_forwarder=True)

    assert MockForwarder.called, "CdpForwarder should have been constructed"
    mock_inject.assert_called_once()
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False, (
        "use_proxy must stay False -- injecting --proxy-server against a closed-book image "
        "with no proxy listening would break every Chrome launch")


def test_forwarder_disabled_skips_the_cdp_forwarder_entirely():
    ctrl = _fake_ctrl()
    task = {"id": "t2", "config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]}
    fake_setup_ctrl = MagicMock()

    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.CdpForwarder") as MockForwarder, \
         patch.object(sandbox, "_verify_launches", return_value=None):
        sandbox._run_config(ctrl, task, enable_cdp_forwarder=False)

    assert not MockForwarder.called, "no flag set -- CdpForwarder must not be touched at all"
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False


# --- env.osworld_eval.evaluate_official / _score -------------------------------------------------

def _task_with_postconfig():
    return {
        "id": "t-eval",
        "evaluator": {
            "func": "infeasible",
            "postconfig": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}],
        },
    }


def test_score_postconfig_cdp_forwarder_sets_use_proxy_false():
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
                            getters=None, metrics=None, cdp_forwarder=fake_cdp_fwd)

    mock_inject.assert_called_once()
    assert fake_setup_ctrl.vm_ip == "127.0.0.1"
    assert fake_setup_ctrl.chromium_port == 54321
    fake_setup_ctrl.setup.assert_called_once()
    _, kwargs = fake_setup_ctrl.setup.call_args
    assert kwargs["use_proxy"] is False, (
        "cdp_forwarder being set must not turn on use_proxy for the postconfig setup call")


def test_score_no_forwarder_leaves_postconfig_untouched():
    fake_setup_ctrl = MagicMock()

    with patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=fake_setup_ctrl), \
         patch("benchmarks.osworld.env.cdp_forwarder.inject_remote_allow_origins") as mock_inject:
        env = MagicMock(action_history=[])
        ev = _task_with_postconfig()["evaluator"]
        osworld_eval._score(env, ev, "infeasible", "http://fake-controller", tempfile.mkdtemp(),
                            getters=None, metrics=None, cdp_forwarder=None)

    assert not mock_inject.called
