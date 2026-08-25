"""Judge unit tests (no desktop, no LLM):  python -m benchmarks.osworld.tests.test_evaluate"""
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
    # desktop_env isn't installed here -> delegation returns None so the runner falls back
    task = {"evaluator": {"func": "compare_table", "result": {"type": "vm_file"}}}
    assert osworld_eval.evaluate_official("http://localhost:5000", task, ["done"]) is None


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
