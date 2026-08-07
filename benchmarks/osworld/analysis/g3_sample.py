"""G3 stratified sample: app x evaluator-class, up to 2 task/cell, deterministic (sorted id).

23 cells (8 apps x strong/medium/weak/infeasible actually present), excluding 'broken'
(evaluator known unreliable) and tasks with no related_apps.

Also excludes chrome_open_tabs/chrome_close_tabs/login: SetupController hard-codes
chromium_port (9222) as a literal host:port, unreachable through our Daytona preview-link
proxy (ENVIRONMENT_ERROR after 15 retries every time). Every cell has a safe alternative.

Also excludes tasks whose evaluator needs a getter/metric missing from the installed
desktop_env (see g0_replication_fidelity.coverage) -- permanently EVAL_ERROR, not transient,
so a run there spends real agent cost for zero scoring data.

Run: python -m benchmarks.osworld.analysis.g3_sample
"""
import collections

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g0_evaluator_audit as g0
from benchmarks.osworld.analysis import g0_replication_fidelity as g0r

PER_CELL = 2
BROKEN_SETUP_TYPES = {"chrome_open_tabs", "chrome_close_tabs", "login"}


def _app_of(t):
    apps = t.get("related_apps") or []
    return apps[0] if apps else "?"


def _cell(t, classes):
    status = g0.task_status(t, classes)
    cls = g0.task_strength(t, classes) if status == "normal" else status
    return (_app_of(t), cls)


def sample():
    classes = g0.classify_all()
    tasks = g0._load_tasks()
    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    ins = [t for t in tasks if set(t.get("related_apps") or []) <= sup]
    unscorable = {a["id"] for a in g0r.coverage()["affected_tasks"]}

    buckets = collections.defaultdict(list)
    for t in ins:
        if t["id"] in unscorable:
            continue
        c = _cell(t, classes)
        if c[1] == "broken" or c[0] == "?":
            continue
        config_types = {s.get("type") for s in (t.get("config") or [])}
        if config_types & BROKEN_SETUP_TYPES:
            continue
        buckets[c].append(t["id"])

    ids = []
    for c in sorted(buckets):
        ids.extend(sorted(buckets[c])[:PER_CELL])
    return ids


def _main():
    ids = sample()
    print(f"k={len(ids)} tasks\n")
    for i in ids:
        print(i)


if __name__ == "__main__":
    _main()
