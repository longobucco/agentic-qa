"""Judge unit tests (no desktop, no LLM):  python -m benchmarks.osworld.tests.test_evaluate"""
import builtins
import json
import tempfile
from pathlib import Path

from benchmarks.osworld import evaluate
from benchmarks.osworld.env import osworld_eval


def _judge_with_state(task, eval_state):
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    (d / "result.json").write_text(json.dumps({"eval_state": eval_state}))
    return evaluate.osworld_check(task, "", None, d)


def test_no_capture_is_eval_error():
    """No eval_state means we have no basis to judge the agent -- must not default to
    FAILURE, or a capture gap would silently contaminate the agent-reliability numbers."""
    task = {"evaluator": {"func": "exact_match", "expected": "x"}}
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    (d / "result.json").write_text(json.dumps({}))          # no eval_state
    assert evaluate.osworld_check(task, "", None, d)["verdict"] == "EVAL_ERROR"


def test_osworld_check_never_returns_success_or_failure():
    """No reimplementation of a real OSWorld metric is verified to agree with the official
    one (desktop_env's exact_match/check_include_exclude are case-sensitive; an earlier
    version of this file wasn't, and could silently disagree). Always EVAL_ERROR, whatever
    the func or captured state."""
    task = {"evaluator": {"func": "exact_match", "expected": "x"}}
    assert _judge_with_state(task, "x")["verdict"] == "EVAL_ERROR"


def test_judge_is_deterministic():
    assert evaluate.JUDGE.is_deterministic is True


def test_reward_to_verdict():
    assert osworld_eval.reward_to_verdict(1.0) == "SUCCESS"
    assert osworld_eval.reward_to_verdict(0.5) == "FAILURE"
    assert osworld_eval.reward_to_verdict(None) == "FAILURE"


def test_official_eval_none_without_desktop_env():
    """Contract: no importable desktop_env -> return None, so the runner falls back to the
    offline checker instead of raising.

    Simulates the missing import rather than relying on desktop_env being absent from the
    environment. The original version just asserted the None and passed only because
    desktop_env wasn't installed yet; once it was, the call ran for real against a
    deliberately incomplete spec and died on KeyError('path') deep inside an official
    getter -- a test that no longer exercised its own stated contract, and would have
    masked a regression in the fallback path."""
    task = {"evaluator": {"func": "compare_table", "result": {"type": "vm_file"}}}
    real_import = builtins.__import__

    def no_desktop_env(name, *args, **kwargs):
        if name.startswith("desktop_env"):
            raise ImportError("simulated: desktop_env not installed")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = no_desktop_env
    try:
        assert osworld_eval.evaluate_official("http://localhost:5000", task, ["done"]) is None
    finally:
        builtins.__import__ = real_import


def test_official_eval_adapter_exposes_attributes_getters_read():
    """The official getters read ports/flags straight off the env object; a missing one
    raises AttributeError *inside* the getter, which the runner files as EVAL_ERROR. A
    missing `vlc_port` alone cost 6 runs across 2 vlc tasks in the G3 campaign, so pin the
    whole set rather than re-discovering them one failed task at a time."""
    env = osworld_eval._EnvAdapter(
        controller=None, controller_url="https://host:5000", action_history=[])
    for attr, expected in (("vm_platform", "Linux"), ("chromium_port", 9222),
                           ("vlc_port", 8080), ("current_use_proxy", False)):
        assert getattr(env, attr) == expected, attr


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
