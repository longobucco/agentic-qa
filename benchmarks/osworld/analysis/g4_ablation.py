"""G4 -- ENVIRONMENT_ERROR convention ablation, re-derived from G3 raw, no new runs -> RQ4.

Recomputes the G3 pass-rate under two conventions and reports the gap between them
(gap-research-plan.md, G4):
  (a) ENVIRONMENT_ERROR excluded -- this repo's default (core/reporting.py)
  (b) ENVIRONMENT_ERROR counted as FAILURE -- upstream OSWorld's convention

EVAL_ERROR is dropped under both: it's our scoring pipeline's own flakiness, not the
ENVIRONMENT_ERROR question this asks. g1_pilot_check.py's `g4_scissor` proved the mechanism on
the 4-run G-1 pilot; this module is the G3-scale deliverable, scoped to the pre-registered
sample (g3_sample.sample()) rather than every directory under results/agent_computer/, which
also holds one-off G0/G0.5 probes and pre-sample pilots (8 on-disk ids not in sample() as of
2026-08-09).

Run: python -m benchmarks.osworld.analysis.g4_ablation
"""
import json

from benchmarks.osworld import config
from benchmarks.osworld.analysis.g3_sample import sample
from core.results import is_run_dir


def _load_verdicts(results_dir=None, system="agent_computer"):
    """{task_id: [verdict, ...]} for every scored run (eval.json present) of a current
    G3-sample task. Tasks not on disk, or excluded from sample() after being run, are simply
    absent -- not zero-filled."""
    results_dir = results_dir or config.RESULTS_DIR
    base = results_dir / system
    sample_ids = set(sample())
    out = {}
    if not base.exists():
        return out
    for tid in sample_ids:
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


def scissor(results_dir=None, system="agent_computer"):
    """Pass-rate under both ENVIRONMENT_ERROR conventions, plus the gap between them and enough
    detail (n, dropped EVAL_ERROR) to tell a rounding artifact from a real methodological fork."""
    by_task = _load_verdicts(results_dir, system)
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
        "n_sample_tasks": len(sample()),
        "n_runs_scored": len(scored_v),
        "n_success": n_ok,
        "n_environment_error": n_env,
        "n_eval_error_dropped": n_eval_error,
        "excluding_env_errors": round(excluding, 4) if excluding is not None else None,
        "counting_env_as_failure": round(counting, 4) if counting is not None else None,
        "scissor_pp": (round(100 * (excluding - counting), 2)
                        if excluding is not None and counting is not None else None),
    }


def _main():
    r = scissor()
    if not r["n_runs_scored"]:
        print("No scored runs yet in the G3 sample -- nothing to derive.")
        return
    print("G4 -- ENVIRONMENT_ERROR convention ablation (RQ4)\n")
    print(f"Sample coverage: {r['n_tasks']}/{r['n_sample_tasks']} G3-sample tasks have at least "
          f"one scored run so far (re-derivable at any point, no new runs needed)")
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


if __name__ == "__main__":
    _main()
