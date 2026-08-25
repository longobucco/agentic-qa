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
  - tasks whose evaluator.result is vm_command_line and whose command makes our controller's
    /execute return non-200 -- desktop_env's own get_vm_command_line then crashes with
    JSONDecodeError on an unconditional `print(response.json())` called before it checks
    status_code (real upstream bug, not ours). Found live 2026-08-09 via g3_anomaly_scan's
    reason_cluster, in two forms:
      - a structural pattern (`_has_unshelled_pipe`): the command is an argv list containing a
        literal "|" with no `shell: true` -- the pipe is passed as a plain string argument,
        never interpreted by a shell. 5/260 in-scope tasks match (all vscode, all the same
        "code --list-extensions | grep <ext>" template); 2 confirmed crashing empirically,
        the other 3 excluded preemptively on the structural match rather than spending agent
        cost to re-discover an already-diagnosed bug.
      - idiosyncratic per-task command bugs that don't fit that pattern (a stray bracket in one
        task's bash, two more whose guest-side failure wasn't isolated) --
        CONFIRMED_BROKEN_VM_COMMAND_TASK_IDS, found empirically, no structural rule to
        generalize from.

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
CONFIRMED_BROKEN_VM_COMMAND_TASK_IDS = {
    "2b9493d7-49b8-493a-a71b-56cd1f4d6908",  # stray "]" in the bash command itself
    "3680a5ee-6870-426a-a997-eba929a0d25c",  # crashes; guest-side cause not isolated
    "ee9a3c83-f437-4879-8918-be5efbb9fac7",  # crashes; guest-side cause not isolated
    "510f64c8-9bcc-4be1-8d30-638705850618",  # crashes with the same JSONDecodeError symptom
    # but via get_vscode_config -> execute_python_command, not get_vm_command_line -- a
    # different code path (our own controller's execute-python endpoint, not a task-spec
    # bug), root cause not fully isolated. 3/3 confirmed EVAL_ERROR.
}


def _has_unshelled_pipe(spec):
    for s in (spec if isinstance(spec, list) else [spec]):
        if not isinstance(s, dict) or s.get("type") != "vm_command_line":
            continue
        cmd = s.get("command")
        if isinstance(cmd, list) and "|" in cmd and not s.get("shell", False):
            return True
    return False


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
        if (t["id"] in unscorable or t["id"] in MISSING_SETUP_CONTROLLER_INIT_TASK_IDS
                or t["id"] in CONFIRMED_BROKEN_VM_COMMAND_TASK_IDS
                or _has_unshelled_pipe(t["evaluator"].get("result"))):
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
