"""Gate C: the sequential decision of X22 stage 3. Zero quota.

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.gates.sequential [--record]

Evaluated ONLY at the pre-registered looks: a task enters a look when all its runs are complete on
both arms, and a look happens when the number of such tasks reaches one of the frozen thresholds.
Between two looks the data are not examined -- the append-only register is what makes it checkable
that they were not.

Three verdicts, two of them arithmetic:
  efficacy -- the exact one-sided McNemar p crosses the frozen nominal alpha of that look;
  futility -- assume EVERY task still to complete turns discordant in the arm's favour; if the p
              with (b + remaining, c) still does not cross the final boundary, the arm cannot win
              and the stage stops. An impossibility, not a probability;
  harm     -- the mirror of efficacy on the strict definition, plus the mean-difference rule.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config
from benchmarks.osworld.gates.calibrate import mcnemar_p

ROOT = Path(__file__).resolve().parents[3]
LOCK = Path(__file__).resolve().parent.parent / "x22_campaign_lock.json"
REGISTER = ROOT / "docs" / "phase3" / "x22_look_register.jsonl"
HARM_MEAN_POINTS = -10.0


def task_outcomes(tree, tasks, runs_per_task, results_dir):
    """{task: (passes, judged)} for the tasks of one arm."""
    out = {}
    root = Path(results_dir) / tree
    for task in tasks:
        d = root / task
        if not d.is_dir():
            continue
        passes = judged = 0
        for p in sorted(d.glob("run_*")):
            # A run is judged iff eval.json carries a verdict (core.results.is_done).
            # infra_error.json is the log of attempts that died before one, so a run holding both
            # is a retry that succeeded and counts. Treating the file as an exclusion would make
            # tasks look permanently incomplete and would delay or distort every look.
            ev = p / "eval.json"
            if not ev.exists():
                continue
            try:
                v = json.loads(ev.read_text()).get("verdict")
            except ValueError:
                continue
            if v not in ("SUCCESS", "FAILURE"):
                continue
            judged += 1
            passes += (v == "SUCCESS")
        out[task] = (passes, judged)
    return out


def paired(lock, results_dir=None, stratum=None):
    """Tasks complete on both arms of stage 3, with both binary definitions.

    Complete means: every declared run judged on BOTH arms. A partially-run task carries no
    information and must never enter a look, otherwise the look is taken on a sample selected by
    how expensive the tasks are.
    """
    results_dir = Path(results_dir) if results_dir else config.RESULTS_DIR
    s3 = lock["stage3"]
    groups = {g["arm"]: g for g in lock["groups"] if g["stage"] == 3}
    tasks = s3["strata"][stratum or s3["primary_stratum"]]
    runs = s3["runs_per_task"]
    base = task_outcomes(groups["a0"]["tree"], tasks, runs, results_dir)
    armd = task_outcomes(groups["e1card2"]["tree"], tasks, runs, results_dir)
    rows = []
    for task in tasks:
        b0, a0_ = base.get(task, (0, 0)), armd.get(task, (0, 0))
        if b0[1] < runs or a0_[1] < runs:
            continue
        rows.append({"task": task,
                     "base_pass": b0[0], "arm_pass": a0_[0], "runs": runs,
                     "gain_base": b0[0] >= 1, "gain_arm": a0_[0] >= 1,
                     "harm_base": b0[0] == runs, "harm_arm": a0_[0] == runs})
    return rows, len(tasks)


def discordant(rows, key_base, key_arm):
    b = sum(1 for r in rows if r[key_arm] and not r[key_base])
    c = sum(1 for r in rows if r[key_base] and not r[key_arm])
    return b, c


def evaluate(lock, results_dir=None, stratum=None):
    s3 = lock["stage3"]
    bounds = s3["boundaries"]
    rows, n_total = paired(lock, results_dir, stratum)
    n = len(rows)
    thresholds = bounds["looks_at_tasks"]
    alphas = bounds["nominal_alpha"]

    reached = [k for k, t in enumerate(thresholds) if n >= t]
    out = {"at": datetime.now(timezone.utc).isoformat(),
           "stratum": stratum or s3["primary_stratum"],
           "tasks_complete_on_both_arms": n, "tasks_in_stratum": n_total,
           "look_thresholds": thresholds, "look_index": (reached[-1] if reached else None),
           "verdict": "no look yet"}
    if not reached:
        out["note"] = (f"{n} of the first threshold {thresholds[0]}: the data are not examined "
                       "before a look")
        return out

    k = reached[-1]
    b, c = discordant(rows, "gain_base", "gain_arm")
    p = mcnemar_p(b, c)
    hb, hc = discordant(rows, "harm_base", "harm_arm")
    p_harm = mcnemar_p(hc, hb)
    mean_diff = (sum(r["arm_pass"] - r["base_pass"] for r in rows)
                 / (len(rows) * s3["runs_per_task"]) * 100) if rows else 0.0
    remaining = n_total - n
    p_best = mcnemar_p(b + remaining, c)

    out.update({"alpha_at_this_look": alphas[k],
                "gain": {"b": b, "c": c, "p_one_sided": round(p, 6)},
                "harm": {"b": hb, "c": hc, "p_one_sided": round(p_harm, 6)},
                "mean_paired_difference_points": round(mean_diff, 2),
                "remaining_tasks": remaining,
                "best_case_p_if_all_remaining_flip": round(p_best, 6)})

    if p <= alphas[k]:
        out["verdict"] = "EFFICACY: the arm beats the baseline at this look"
    elif p_harm <= alphas[k] or mean_diff <= HARM_MEAN_POINTS:
        out["verdict"] = "HARM: stop; every negative flip is re-run at 5 runs before being called harm"
    elif p_best > alphas[-1]:
        out["verdict"] = ("FUTILITY: even if every remaining task flipped in the arm's favour the "
                          "final boundary would not be crossed; the arm cannot win")
    elif k == len(thresholds) - 1:
        out["verdict"] = "INCONCLUSIVE at the final look: reported as such, not as 'no effect'"
    else:
        out["verdict"] = "CONTINUE"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m benchmarks.osworld.gates.sequential")
    ap.add_argument("--record", action="store_true", help="append the verdict to the look register")
    ap.add_argument("--stratum", default=None)
    ap.add_argument("--results-dir", default=None)
    a = ap.parse_args(argv)
    lock = json.loads(LOCK.read_text())
    out = evaluate(lock, a.results_dir, a.stratum)
    print(json.dumps(out, indent=2))
    if a.record:
        REGISTER.parent.mkdir(parents=True, exist_ok=True)
        with REGISTER.open("a") as fh:
            fh.write(json.dumps(out) + "\n")
        print(f"\nappended to {REGISTER}")
    return 0 if out["verdict"] in ("no look yet", "CONTINUE") else 2


if __name__ == "__main__":
    raise SystemExit(main())
