"""G0 empirical brittleness probe (LIVE -- needs a sandbox; zero agent cost).

Criterion validation of the strength rubric (g0_evaluator_audit). Run a task's
config to set the initial state, do NOT act, then score with the official
evaluator. A well-formed evaluator returns FAILURE (the task was not done); a
SUCCESS on the untouched state is a measured false positive -- the check passes
without the task being performed. If weak/medium funcs false-positive more than
strong, the a-priori rubric is empirically confirmed; if not, it is challenged.

No Claude agent runs, so this costs only sandbox time. One sandbox is reused
across the sample: each task is scored on its own config'd state, so cross-task
residue is unlikely to matter -- but this is a probe, not a graded run.

Usage (provision a sandbox first, then pass its controller URL):
  python -m benchmarks.osworld.env.sandbox up
  python -m benchmarks.osworld.analysis.g0_brittleness <controller_url> [per_class]
"""
import sys

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g0_evaluator_audit as g0
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.env import sandbox as sb
from benchmarks.osworld.env.controller import Controller

# prefer these for the sample: their config setup is the most reliable live, so a
# FAILURE/SUCCESS reflects the evaluator, not a broken setup
RELIABLE_APPS = {"libreoffice_calc", "libreoffice_writer", "libreoffice_impress", "gimp", "vlc"}


def sample(per_class=5):
    classes = g0.classify_all()
    sup = config.SUPPORTED_APPS
    inscope = [t for t in g0._load_tasks()
               if set(t.get("related_apps") or []) <= sup
               and g0.task_status(t, classes) == "normal"]
    out = {}
    for strength in ("strong", "medium", "weak"):
        pool = [t for t in inscope if g0.task_strength(t, classes) == strength]
        # reliable-config apps first, then deterministic by id
        pool.sort(key=lambda t: (not set(t.get("related_apps") or []) <= RELIABLE_APPS, t["id"]))
        out[strength] = pool[:per_class]
    return out


def probe(controller_url, task, strength):
    """Run config, do not act, score. SUCCESS => the evaluator passed a task that
    was never performed => a false positive."""
    ctrl = Controller(controller_url)
    setup_err = sb._run_config(ctrl, task)
    if setup_err:
        return {"id": task["id"], "strength": strength, "outcome": "ENVIRONMENT_ERROR",
                "detail": setup_err}
    try:
        reward = osworld_eval.evaluate_official(controller_url, task, action_history=[])
    except Exception as e:
        return {"id": task["id"], "strength": strength, "outcome": "EVAL_RAISED",
                "detail": f"{type(e).__name__}: {e}"}
    verdict = osworld_eval.reward_to_verdict(reward)
    return {"id": task["id"], "strength": strength, "outcome": "scored",
            "verdict": verdict, "reward": reward, "false_positive": verdict == "SUCCESS"}


def run(controller_url, per_class=5):
    samp = sample(per_class)
    results = []
    for strength in ("strong", "medium", "weak"):
        for t in samp[strength]:
            r = probe(controller_url, t, strength)
            results.append(r)
            print(r)
    return results


def summarize(results):
    """False-positive rate per strength, over tasks that actually scored (drop
    ENVIRONMENT_ERROR / EVAL_RAISED -- those measure setup/import, not the oracle)."""
    from collections import Counter
    scored = Counter()
    fp = Counter()
    for r in results:
        if r["outcome"] != "scored":
            continue
        scored[r["strength"]] += 1
        fp[r["strength"]] += int(r["false_positive"])
    print("\nfalse-positive rate on the no-op state (SUCCESS without the task done):")
    for s in ("strong", "medium", "weak"):
        n = scored[s]
        print(f"  {s:<8}{fp[s]}/{n}" + (f"  ({100*fp[s]/n:.0f}%)" if n else "  (no scored tasks)"))
    dropped = [r for r in results if r["outcome"] != "scored"]
    if dropped:
        print(f"\ndropped (not scored): {len(dropped)}")
        for r in dropped:
            print(f"  {r['id']}  {r['strength']}  {r['outcome']}: {r.get('detail','')[:80]}")


def _main(argv):
    if not argv:
        print(__doc__)
        return
    per_class = int(argv[1]) if len(argv) > 1 else 5
    results = run(argv[0], per_class)
    summarize(results)


if __name__ == "__main__":
    _main(sys.argv[1:])
