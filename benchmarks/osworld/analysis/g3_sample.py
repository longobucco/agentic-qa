"""G3 stratified sample: app x evaluator-class cell, 2 tasks/cell, deterministic (sorted id).

23 cells (8 apps x strong/medium/weak/infeasible present in the data). Excludes 'broken'
evaluators and tasks with no related_apps.

Further exclusions, all permanent EVAL_ERROR/ENVIRONMENT_ERROR causes rather than transient
flakiness -- a run there buys no scoring data at real agent cost:
  - chrome_open_tabs/chrome_close_tabs/login (BROKEN_SETUP_TYPES): SetupController hardcodes
    chromium_port (9222) as host:port, unreachable through the Daytona preview-link proxy.
  - getters/metrics absent from the installed desktop_env (g0_replication_fidelity.coverage).
    16/369 tasks, 12 in scope.
  - vlc_playing_info (UNROUTABLE_GETTER_TYPES): same unreachable-port issue, this time on
    env.vlc_port (8080). 2/369 tasks.
  - postconfig step types SetupController doesn't implement, e.g. chrome_inject_js
    (MISSING_SETUP_CONTROLLER_INIT_TASK_IDS). 2/369 tasks.

Run: python -m benchmarks.osworld.analysis.g3_sample
"""
import collections

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g0_evaluator_audit as g0
from benchmarks.osworld.analysis import g0_replication_fidelity as g0r
from benchmarks.osworld.analysis.g0_replication_fidelity import _task_evaluator_symbols

PER_CELL = 2
BROKEN_SETUP_TYPES = {"chrome_open_tabs", "chrome_close_tabs", "login"}
UNROUTABLE_GETTER_TYPES = {"vlc_playing_info"}
MISSING_SETUP_CONTROLLER_INIT_TASK_IDS = {
    "030eeff7-b492-4218-b312-701ec99ee0cc", "2ae9ba84-3a0d-4d4c-8338-3a1478dc5fe3",
}


def _app_of(t):
    apps = t.get("related_apps") or []
    return apps[0] if apps else "?"


def _cell(t, classes):
    status = g0.task_status(t, classes)
    cls = g0.task_strength(t, classes) if status == "normal" else status
    return (_app_of(t), cls)


def _eligible(classes, tasks):
    """Every in-scope task that clears all the permanent-failure exclusions above, no
    per-cell cap. `sample()` and `full()` both filter from this; `sample()` additionally
    caps at PER_CELL per (app, class)."""
    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    ins = [t for t in tasks if set(t.get("related_apps") or []) <= sup]
    unscorable = {a["id"] for a in g0r.coverage()["affected_tasks"]}

    out = []
    for t in ins:
        if t["id"] in unscorable or t["id"] in MISSING_SETUP_CONTROLLER_INIT_TASK_IDS:
            continue
        c = _cell(t, classes)
        if c[1] == "broken" or c[0] == "?":
            continue
        config_types = {s.get("type") for s in (t.get("config") or [])}
        if config_types & BROKEN_SETUP_TYPES:
            continue
        _, getter_types = _task_evaluator_symbols(t)
        if getter_types & UNROUTABLE_GETTER_TYPES:
            continue
        out.append(t)
    return out


def sample():
    classes = g0.classify_all()
    tasks = g0._load_tasks()
    eligible = _eligible(classes, tasks)

    buckets = collections.defaultdict(list)
    for t in eligible:
        buckets[_cell(t, classes)].append(t["id"])

    ids = []
    for c in sorted(buckets):
        ids.extend(sorted(buckets[c])[:PER_CELL])
    return ids


def full():
    """Every eligible task, uncapped -- the population G3's sample draws from. Used by
    G3-full (gap-research-plan.md) to run the whole dataset instead of the stratified k."""
    classes = g0.classify_all()
    tasks = g0._load_tasks()
    return sorted(t["id"] for t in _eligible(classes, tasks))


def _main():
    ids = sample()
    print(f"k={len(ids)} tasks\n")
    for i in ids:
        print(i)


if __name__ == "__main__":
    _main()
