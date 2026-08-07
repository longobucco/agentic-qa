"""Fallback when the official evaluator is unavailable (desktop_env not importable, or it
raised). Never produces SUCCESS/FAILURE: a from-scratch reimplementation of even the simplest
OSWorld metrics is not verified to agree with the real ones (confirmed live: desktop_env's
exact_match/check_include_exclude are case-sensitive, an earlier version of this file was not,
so it could silently disagree with the official evaluator). Always EVAL_ERROR -- an honest "no
verified way to score this" backed by whatever state we did capture, not a guess.

core.judge.Judge is required by core.run.Benchmark; osworld's runner is self_eval=True, so
core.run never actually calls this -- see runners/agent_computer.py::_score.
"""
import json

from core.judge import Judge


def _read_captured(out):
    p = out / "result.json"
    return json.loads(p.read_text()) if p.exists() else {}


def osworld_check(task, answer, ref, out):
    value = _read_captured(out).get("eval_state")
    reason = "no eval_state captured" if value is None else f"eval_state={value!r}"
    return {"verdict": "EVAL_ERROR", "reason": reason}


JUDGE = Judge(fn=osworld_check, is_deterministic=True)
