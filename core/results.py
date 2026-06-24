"""Results directory IO: the run-dir layout, result/eval writes, resume, and aggregation.

One *trajectory* = one *run*. N runs per task (agent re-rolls) get sibling `run_<k>` dirs so
agent variance is recoverable; repeated judging of a single trajectory (judge variance) is
folded into that run's `eval.json`.

    <results_dir>/<system>/<task_id>/run_<k>/
        result.json       written by the runner: answer + task metadata (incl. bucket)
        agent_output.txt  raw claude transcript (runner)
        step_*.png ...    optional screenshots (runner)
        eval.json         written here after judging: verdict [+ judge_runs for variance]
"""
import json


def run_dir(results_dir, system, task_id, run_idx):
    return results_dir / system / task_id / f"run_{run_idx}"


def write_result(out, record):
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(record, indent=2))


def write_output(out, text):
    out.mkdir(parents=True, exist_ok=True)
    (out / "agent_output.txt").write_text(text or "")


def write_eval(out, record):
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval.json").write_text(json.dumps(record, indent=2))


def is_done(out, *, need_eval=True):
    """Resume check for one run: judged (eval.json) or, with --no-eval, merely run."""
    return (out / ("eval.json" if need_eval else "result.json")).exists()


def aggregate_judge(verdicts):
    """Fold repeated judge verdicts on ONE trajectory into a single eval record.

    Majority vote; ties break to FAILURE (the strict choice). Every pass is retained under
    `judge_runs` and the success fraction under `judge_success_rate` so judge variance stays
    inspectable. A single pass returns a plain `{verdict, reason}` (no variance fields).
    """
    succ = [v for v in verdicts if v["verdict"] == "SUCCESS"]
    verdict = "SUCCESS" if len(succ) * 2 > len(verdicts) else "FAILURE"
    if verdict == "SUCCESS":
        reason = succ[0]["reason"]
    else:
        fails = [v for v in verdicts if v["verdict"] != "SUCCESS"]
        reason = fails[0]["reason"] if fails else (verdicts[0]["reason"] if verdicts else "")
    rec = {"verdict": verdict, "reason": reason}
    if len(verdicts) > 1:
        rec["judge_success_rate"] = len(succ) / len(verdicts)
        rec["judge_runs"] = verdicts
    return rec
