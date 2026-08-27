"""G4 -- ENVIRONMENT_ERROR convention ablation, re-derived from raw, no new runs -> RQ4.

Recomputes the pass-rate under two conventions and reports the gap between them
(gap-research-plan.md, G4):
  (a) ENVIRONMENT_ERROR excluded -- this repo's default (core/reporting.py)
  (b) ENVIRONMENT_ERROR counted as FAILURE -- upstream OSWorld's convention

EVAL_ERROR is dropped under both: it's our scoring pipeline's own flakiness, not the
ENVIRONMENT_ERROR question this asks. g1_pilot_check.py's `g4_scissor` proved the mechanism on
the 4-run G-1 pilot; `scissor()` here is the original G3-scale deliverable, scoped to the
pre-registered sample (g3_sample.sample()) rather than every directory under
results/agent_computer/, which also holds one-off G0/G0.5 probes and pre-sample pilots (8
on-disk ids not in sample() as of 2026-08-09).

Two scopes, by design, not by drift:
  scissor()      -- the 46-task pre-registered sample. Run 2026-08-25: scissor = +0.93pp
                     (41.1% excluding vs 40.2% counting) -- negligible at this scale, only 3
                     ENVIRONMENT_ERROR observed.
  scissor_full()  -- added 2026-08-25, every runnable task (327/358 with a scored run as of
                     that date), not just the pre-registered sample. scissor = +4.28pp (49.1%
                     vs 44.9%) -- NOT negligible at this scope. The difference from the small
                     sample isn't noise: the full population's 72 ENVIRONMENT_ERROR (vs 3 in
                     the pre-registered 46) are disproportionately the chrome_open_tabs/
                     googledrive-credential tasks the 2026-08-16 scope-widening decision
                     deliberately kept in the run queue despite g3_sample.py already knowing
                     them to be structurally broken. Convention choice only stops being a
                     rounding artifact once that broken tail is in the denominator -- report
                     BOTH numbers together, not just the pre-registered one, once a campaign
                     has gone past the original sample's scope.

Run: python -m benchmarks.osworld.analysis.g4_ablation           # both scopes
     python -m benchmarks.osworld.analysis.g4_ablation --sample  # pre-registered scope only
"""
import json
import sys

from benchmarks.osworld import config
from benchmarks.osworld.analysis.g3_sample import sample
from core.results import is_run_dir


def _load_verdicts(ids, results_dir=None, system="agent_computer"):
    """{task_id: [verdict, ...]} for every scored run (eval.json present) of a task in `ids`.
    Tasks not on disk are simply absent -- not zero-filled."""
    results_dir = results_dir or config.RESULTS_DIR
    base = results_dir / system
    out = {}
    if not base.exists():
        return out
    for tid in ids:
        tdir = base / tid
        if not tdir.exists():
            continue
        verdicts = []
        for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and is_run_dir(p.name)):
            ev = rdir / "eval.json"
            if not ev.exists():
                continue
            try:
                verdicts.append(json.loads(ev.read_text()).get("verdict"))
            except Exception:
                continue
        if verdicts:
            out[tid] = verdicts
    return out


def _scissor_over(ids, results_dir=None, system="agent_computer"):
    """Pass-rate under both ENVIRONMENT_ERROR conventions over the given task-id population,
    plus the gap between them and enough detail (n, dropped EVAL_ERROR) to tell a rounding
    artifact from a real methodological fork."""
    ids = list(ids)
    by_task = _load_verdicts(ids, results_dir, system)
    all_v = [v for vs in by_task.values() for v in vs]
    scored_v = [v for v in all_v if v != "EVAL_ERROR"]
    n_env = sum(v == "ENVIRONMENT_ERROR" for v in scored_v)
    n_ok = sum(v == "SUCCESS" for v in scored_v)
    n_excl_denom = len(scored_v) - n_env
    n_eval_error = len(all_v) - len(scored_v)

    excluding = (n_ok / n_excl_denom) if n_excl_denom else None
    counting = (n_ok / len(scored_v)) if scored_v else None

    return {
        "n_tasks": len(by_task),
        "n_population_tasks": len(ids),
        "n_runs_scored": len(scored_v),
        "n_success": n_ok,
        "n_environment_error": n_env,
        "n_eval_error_dropped": n_eval_error,
        "excluding_env_errors": round(excluding, 4) if excluding is not None else None,
        "counting_env_as_failure": round(counting, 4) if counting is not None else None,
        "scissor_pp": (round(100 * (excluding - counting), 2)
                        if excluding is not None and counting is not None else None),
    }


def scissor(results_dir=None, system="agent_computer"):
    """Original G4 scope: the 46-task pre-registered sample (gap-research-plan.md)."""
    return _scissor_over(sample(), results_dir, system)


def scissor_full(results_dir=None, system="agent_computer"):
    """Extended scope, added 2026-08-25: every runnable task, not just the pre-registered
    sample -- see the module docstring for why the two scopes give materially different
    answers rather than one refining the other."""
    from benchmarks.osworld import tasks as tasks_mod
    return _scissor_over((t["id"] for t in tasks_mod.load_tasks()), results_dir, system)


def _print(label, r, population_label):
    if not r["n_runs_scored"]:
        print(f"{label}: no scored runs yet -- nothing to derive.")
        return
    print(f"{label}\n")
    print(f"Coverage: {r['n_tasks']}/{r['n_population_tasks']} {population_label} have at "
          f"least one scored run so far (re-derivable at any point, no new runs needed)")
    print(f"Run-evals: {r['n_runs_scored']} scored ({r['n_eval_error_dropped']} EVAL_ERROR "
          f"dropped from both conventions -- pipeline flakiness, not the question G4 asks)")
    print(f"  ENVIRONMENT_ERROR: {r['n_environment_error']}")
    print()
    print(f"  excluding ENVIRONMENT_ERROR (this repo's default): "
          f"{100 * r['excluding_env_errors']:.1f}%")
    print(f"  counting ENVIRONMENT_ERROR as FAILURE (upstream convention): "
          f"{100 * r['counting_env_as_failure']:.1f}%")
    print(f"  scissor: {r['scissor_pp']:+.2f} percentage points")
    if r["n_environment_error"] == 0:
        print("\n  (0 ENVIRONMENT_ERROR observed so far -> the two conventions currently "
              "coincide; not evidence they always will, only that this sample hasn't hit the "
              "config-setup failure mode yet -- re-run as the campaign progresses.)")


def _main():
    print("G4 -- ENVIRONMENT_ERROR convention ablation (RQ4)\n")
    _print("-- pre-registered sample --", scissor(), "G3-sample tasks")
    if "--sample" not in sys.argv:
        print()
        _print("-- full runnable population --", scissor_full(), "runnable tasks")


if __name__ == "__main__":
    _main()
