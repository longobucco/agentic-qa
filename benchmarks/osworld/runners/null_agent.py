"""Null agent: the task is set up exactly as for a model run, nothing acts on the desktop, the
answer is DONE and the official evaluator scores the untouched end state. Uses no model quota.

A sound environment scores ~0 here: every SUCCESS is a task whose checker already passes on the
initial state (or on DONE alone), i.e. a fidelity or checker defect to record, not an agent win.
Same lifecycle as the model runners: setup, 60 s settle, 20 s before evaluate, official scoring.
"""
from datetime import datetime, timezone

from benchmarks.osworld import config, tasks
from benchmarks.osworld.runners.common import (
    _a11y_health, _bounded, _capture_eval_state, _environment_error_rec, _provenance, _score,
    protocol_wait,
)
from core import results as results_io

ANSWER = "DONE"


def run(task, *, env, out, refs=None, dry=False):
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    setup_error = getattr(env, "setup_error", None)
    if dry:
        print(f"DRY-RUN null agent on {task['id']}: no actions, answer {ANSWER!r}")
        return None
    if setup_error:
        rec = _environment_error_rec(task, setup_error)
        rec["result"]["provenance"] = _provenance(task, ctrl, started_at)
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""
    (out / "eval.json").unlink(missing_ok=True)
    protocol_wait(config.POST_SETUP_WAIT_S)   # upstream: sleep 60 after reset
    results_io.write_output(out, ANSWER)
    protocol_wait(config.PRE_EVAL_WAIT_S)     # upstream: sleep 20 before evaluate
    rec = _bounded("scoring", _score, ctrl, task, ANSWER, out)
    eval_state = _bounded("eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    provenance = {**_provenance(task, ctrl, started_at), "model_requested": None,
                  "agent_runtime": "null_agent"}
    results_io.write_result(out, {
        "id": task["id"], "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"], "answer": ANSWER,
        "eval_state": eval_state, "provenance": provenance,
        "agent_steps_used": 0, "agent_non_computer_tool_calls": [],
        **_a11y_health(ctrl),
    })
    results_io.write_eval(out, {"id": task["id"], **rec})
    return ANSWER
