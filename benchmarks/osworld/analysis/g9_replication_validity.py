"""G9 -- which verdicts are invalidated by OUR defects, and what has to be re-run.

Re-derived from raw, no new runs, zero cost. G8 asks "what broke"; this asks the narrower
question a replication has to answer before quoting any number: *whose fault was it, and is
the verdict still usable?* Every scored run lands in exactly one bucket:

    VALID          the agent ran under the intended conditions and the oracle delivered a
                   verdict -- counts towards the pass rate
    INVALID_OURS   a harness/scoring defect on our side determined the outcome. The recorded
                   verdict must not be used; the task needs re-running once the defect is
                   fixed. This is the list `--ids` is meant to consume.
    EXCLUDED       neither the agent's nor our doing (upstream data rot, an oracle that needs
                   credentials we don't have). Not re-runnable; must be declared, not hidden.
    UNRESOLVED     the evidence on disk can't decide between "the agent left the desktop in a
                   state the oracle chokes on" and "our image never produced that state".
                   Needs one live no-op probe (g0_brittleness's method, zero agent cost)
                   before it can be moved into one of the buckets above.

The four causes under INVALID_OURS, in descending blast radius:

  oracle_url_scheme    _EnvAdapter hands upstream's getters vm_ip/server_port split out of an
                       https:// controller URL, so every getter that interpolates its own
                       "http://{ip}:{port}" talks plain HTTP to port 443. The affected getter
                       names are DERIVED by scanning the installed desktop_env source, not
                       hardcoded, so this stays honest across upgrades.
  oracle_lib_version   the task set is pinned to an upstream commit (data/download_data.py::
                       UPSTREAM_COMMIT) but the evaluator library is whatever `desktop_env`
                       version is installed. Tasks referencing a metric/getter that version
                       doesn't have die with AttributeError / unexpected-keyword TypeError.
  oracle_unroutable    the getter reaches for a guest port the sandbox doesn't publish (VLC's
                       8080, Chrome's 9222): connection reset, not a wrong answer.
  oracle_getter_reached_zero
                       a scored 0.0 on a task whose evidence path runs through one of those
                       broken getters, where the agent did NOT answer FAIL -- so the getter was
                       really called and the metric scored whatever it got back instead of the
                       desktop's actual state.
  agent_truncated      the agent was cut off (API error / rate limit) and the run was scored
                       anyway -- a FAILURE that says nothing about the agent.

Run: python -m benchmarks.osworld.analysis.g9_replication_validity [--system X] [--json]
     python -m benchmarks.osworld.analysis.g9_replication_validity --write-ids <path>
"""
import collections
import json
import pathlib
import re
import sys

from benchmarks.osworld import config, tasks as osw_tasks
from core.results import is_run_dir

# --- cause signatures, matched against eval.json's reason ---------------------------------
# Version skew between the pinned task set and the installed evaluator library. Both shapes
# mean the same thing: the task asks for a symbol this desktop_env doesn't have.
_LIB_VERSION_RE = re.compile(
    r"AttributeError: module 'desktop_env[.\w]*' has no attribute"
    r"|TypeError: \w+\(\) got an unexpected keyword argument")
# A getter that returned None because the state it wanted was absent, then crashed writing or
# joining it. Ambiguous by construction: absent because the agent didn't create it, or absent
# because our image never had it.
_MISSING_STATE_RE = re.compile(
    r"TypeError: a bytes-like object is required, not 'NoneType'"
    r"|TypeError: expected str, bytes or os\.PathLike object, not NoneType")
_UNROUTABLE_RE = re.compile(r"ConnectionError|ConnectionResetError|Connection refused"
                            r"|Max retries exceeded|NewConnectionError")
_URL_SCHEME_RE = re.compile(r"JSONDecodeError")

# ENVIRONMENT_ERROR causes. Setup that needs credentials we deliberately don't ship, and setup
# that fetches something upstream has since moved -- neither is re-runnable as-is.
_NEEDS_CREDENTIALS_RE = re.compile(r"client_secrets|_googledrive_setup|credentials\.json")
_UPSTREAM_GONE_RE = re.compile(r"4\d\d Client Error|Not Found")


def affected_getters(package_dir=None):
    """Getter names that build their own 'http://{host}:{port}' URL instead of going through
    env.controller -- derived from the installed desktop_env source, so an upgrade that fixes
    them shrinks this set instead of silently leaving a stale list behind."""
    if package_dir is None:
        try:
            import desktop_env
            package_dir = pathlib.Path(desktop_env.__file__).parent
        except Exception:
            return set()
    getters = pathlib.Path(package_dir) / "evaluators" / "getters"
    out = set()
    if not getters.is_dir():
        return out
    for src in getters.glob("*.py"):
        text = src.read_text(errors="replace")
        for m in re.finditer(r"def (get_\w+)\(([\s\S]*?)(?=\ndef |\Z)", text):
            if "http://{" in m.group(2):
                out.add(m.group(1))
    return out


def _spec_getters(task):
    """The get_* functions a task's evaluator will actually call."""
    ev = task.get("evaluator") or {}
    out = set()
    for key in ("result", "expected"):
        spec = ev.get(key)
        for s in (spec if isinstance(spec, list) else [spec]):
            if isinstance(s, dict) and s.get("type"):
                out.add("get_" + s["type"])
    return out


def _answered_fail(result):
    """Did the agent itself declare the task infeasible? Then the oracle never ran a getter."""
    return (result.get("answer") or "").strip().upper().startswith("FAIL")


def _classify(task, result, ev, broken_getters):
    """(bucket, cause) for one scored run."""
    verdict = ev.get("verdict")
    reason = ev.get("reason") or ""

    # An agent that never finished can't be judged, whatever the oracle then computed.
    if result.get("agent_api_error_status") or result.get("agent_is_error"):
        if verdict in ("SUCCESS", "FAILURE"):
            return "INVALID_OURS", "agent_truncated"

    if verdict == "ENVIRONMENT_ERROR":
        if _NEEDS_CREDENTIALS_RE.search(reason):
            return "EXCLUDED", "env_needs_credentials"
        if _UPSTREAM_GONE_RE.search(reason):
            return "EXCLUDED", "env_upstream_gone"
        return "INVALID_OURS", "env_setup_flake"

    if verdict == "EVAL_ERROR":
        if _LIB_VERSION_RE.search(reason):
            return "INVALID_OURS", "oracle_lib_version"
        if _URL_SCHEME_RE.search(reason):
            return "INVALID_OURS", "oracle_url_scheme"
        if _UNROUTABLE_RE.search(reason):
            return "INVALID_OURS", "oracle_unroutable"
        if _MISSING_STATE_RE.search(reason):
            return "UNRESOLVED", "oracle_missing_state"
        return "INVALID_OURS", "oracle_other"

    # SUCCESS/FAILURE on a task whose evaluator depends on a getter we know is broken here.
    # Whether that taints the verdict turns on ONE thing: was the getter actually reached?
    # evaluate_official short-circuits to 0.0 the moment the agent's own last answer is FAIL
    # (osworld_eval.py::_last_is_fail), before any getter runs -- so a FAIL-answer 0.0 is the
    # agent declaring the task infeasible, which is real agent behaviour and stays VALID.
    # Getting this backwards costs a re-run campaign: all 15 such FAILUREs in the Sonnet-5
    # cohort are FAIL-answers, and reading them as oracle artifacts would have queued six
    # perfectly good tasks for a pointless re-run.
    if _spec_getters(task) & broken_getters and not _answered_fail(result):
        return "INVALID_OURS", ("oracle_getter_reached_zero" if verdict == "FAILURE"
                                else "oracle_getter_reached_success")
    return "VALID", "scored"


def audit(results_dir=None, system=None, tasks_by_id=None, broken_getters=None):
    """`tasks_by_id` / `broken_getters` are injection points for the tests; in normal use both
    come from the pinned task set and the installed desktop_env."""
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    tasks = tasks_by_id if tasks_by_id is not None else {t["id"]: t for t in osw_tasks.load_tasks()}
    broken = affected_getters() if broken_getters is None else set(broken_getters)

    runs = []          # (task_id, run_name, bucket, cause)
    unscored = []      # ran (or tried) but never got an eval.json -- resume retries these
    if base.exists():
        for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
            task = tasks.get(tdir.name)
            if task is None:
                continue          # out of the current scope (e.g. app filter changed)
            for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and is_run_dir(p.name)):
                evf, resf = rdir / "eval.json", rdir / "result.json"
                if not evf.exists():
                    if resf.exists() or (rdir / "infra_error.json").exists():
                        unscored.append(f"{tdir.name}/{rdir.name}")
                    continue
                ev = json.loads(evf.read_text())
                res = json.loads(resf.read_text()) if resf.exists() else {}
                bucket, cause = _classify(task, res, ev, broken)
                runs.append((tdir.name, rdir.name, bucket, cause))

    by_cause = collections.defaultdict(lambda: {"runs": 0, "tasks": set()})
    by_bucket = collections.Counter()
    for tid, _run, bucket, cause in runs:
        by_bucket[bucket] += 1
        by_cause[cause]["runs"] += 1
        by_cause[cause]["tasks"].add(tid)

    # Tasks to re-run: every task with at least one INVALID_OURS run. Re-running is per task
    # (`run.py --ids`), not per run, so the unit here is the task.
    rerun = sorted({tid for tid, _r, b, _c in runs if b == "INVALID_OURS"})
    unresolved = sorted({tid for tid, _r, b, _c in runs if b == "UNRESOLVED"})
    excluded = sorted({tid for tid, _r, b, _c in runs if b == "EXCLUDED"})

    # Tasks that are entirely clean -- no invalid run at all -- are the only ones a pass rate
    # may quote as-is.
    dirty = set(rerun) | set(unresolved)
    valid_tasks = sorted({tid for tid, _r, b, _c in runs if b == "VALID"} - dirty)

    return {
        "system": system,
        "n_runs_scored": len(runs),
        "n_runs_unscored": len(unscored),
        "buckets": dict(by_bucket),
        "causes": {c: {"runs": d["runs"], "tasks": sorted(d["tasks"])}
                   for c, d in sorted(by_cause.items(), key=lambda kv: -kv[1]["runs"])},
        "broken_getters": sorted(broken),
        "rerun_task_ids": rerun,
        "unresolved_task_ids": unresolved,
        "excluded_task_ids": excluded,
        "clean_task_ids": valid_tasks,
    }


def _main():
    system = config.system_from_argv()
    rep = audit(system=system)
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2))
        return
    if "--write-ids" in sys.argv:
        out = pathlib.Path(sys.argv[sys.argv.index("--write-ids") + 1])
        out.write_text("\n".join(rep["rerun_task_ids"]) + "\n")
        print(f"{len(rep['rerun_task_ids'])} task id(s) -> {out}")
        return

    print(f"G9 replication validity -- {rep['system']}   "
          f"{rep['n_runs_scored']} scored run(s), {rep['n_runs_unscored']} unscored "
          f"(resume retries those)\n")
    total = max(rep["n_runs_scored"], 1)
    for b in ("VALID", "INVALID_OURS", "UNRESOLVED", "EXCLUDED"):
        n = rep["buckets"].get(b, 0)
        print(f"  {b:16s}{n:5d}  {100*n/total:5.1f}%")

    print("\nCAUSE".ljust(38) + "runs  tasks")
    for cause, d in rep["causes"].items():
        if cause == "scored":
            continue
        print(f"  {cause:34s}{d['runs']:5d}{len(d['tasks']):7d}")

    print(f"\nRE-RUN once the defect is fixed: {len(rep['rerun_task_ids'])} task(s)")
    print(f"NEEDS A LIVE NO-OP PROBE first:  {len(rep['unresolved_task_ids'])} task(s)"
          f"   {', '.join(t[:8] for t in rep['unresolved_task_ids'])}")
    print(f"NOT RE-RUNNABLE, declare instead: {len(rep['excluded_task_ids'])} task(s)"
          f"   {', '.join(t[:8] for t in rep['excluded_task_ids'])}")
    print(f"\nclean tasks (verdicts usable as-is): {len(rep['clean_task_ids'])}")
    print(f"\n{len(rep['broken_getters'])} getter(s) in the installed desktop_env build their "
          f"own http:// URL:\n  {', '.join(rep['broken_getters'])}")


if __name__ == "__main__":
    _main()
