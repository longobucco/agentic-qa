"""Campaign driver for the official-protocol OSWorld-Verified campaigns (361 tasks x 5 runs) on
the kvm backend (docs/superpowers/plans/2026-09-24-osworld-official-fidelity.md, Task 10):
parallel shards, quota-safe, preflight first.

    ARM=sonnet|astra PARALLEL=K MAX_HOURS=H .venv/bin/python scripts/g_official361_driver.py
    ... --dry-run    # print the planned batches, run nothing

Environment. The driver exports the protocol environment itself (OSW_PROTOCOL=official,
OSW_BACKEND=kvm, OSW_POPULATION=verified361, OSW_MAX_STEPS=100, 1920x1080, and per arm the
pinned model, effort and the `protocol361` suffix) BEFORE anything imports
benchmarks.osworld.config -- config reads the environment at import time, and an earlier driver
in this project set it afterwards and silently ran the wrong configuration. For ARM=astra,
OSW_ASTRA_REASONING_EFFORT must be set by the caller (it is a campaign decision, not a default)
and the campaign lock is astra_official361_lock.json. The OSW_KVM_* host settings come from the
caller's environment; the runner preflight validates them.

Preflight first: the arm's Runner preflight (benchmark.build(): the kvm host check, then the
runner's own) runs once in-process, and any failure exits before a single VM is started.

Rounds. Each round takes the pending (task, run) units -- no eval.json yet, via core.results.
is_done, exactly the check run.py itself uses to resume -- groups their tasks into batches of 5,
and starts up to PARALLEL `python -m benchmarks.osworld.run --system S --runs 5 --concurrency 1
--ids <batch>` children at once: disjoint task sets, so K official VMs run side by side and no two
children ever touch the same run dir. No --force: resume is free, a restarted driver re-derives
the pending set from disk. After every round, `decide` looks at what the children just wrote:
  - "stop" (exit 3): a child exited non-zero without writing any infra_error.json (its own
    preflight refused, or it crashed before any unit), or some unit has accumulated >= 3
    infra_error records that are not RATE_LIMITED (a non-quota failure that would otherwise be
    retried forever, since it never produces an eval.json);
  - "backoff": >= 50% of the units just attempted ended RATE_LIMITED -- sleep 1800 s (same poll
    as the open-book driver: quota resets are hours apart) and try again;
  - "continue" otherwise.
Tool-surface violations are not infra errors: they write a terminal FAILURE eval.json and are
never retried. MAX_HOURS is checked between rounds. Everything (the driver's own lines and the
children's output) goes to scripts/g_official361_<arm>.log.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:   # run as a script: make `benchmarks` / `core` importable
    sys.path.insert(0, str(_ROOT))

RUNS = 5
BATCH_TASKS = 5
BACKOFF_S = 1800
RATE_LIMIT_THRESHOLD = 0.5
MAX_NON_QUOTA_INFRA_ERRORS = 3

_COMMON_ENV = {
    "OSW_PROTOCOL": "official",
    "OSW_BACKEND": "kvm",
    "OSW_POPULATION": "verified361",
    "OSW_MAX_STEPS": "100",
    "OSW_SCREEN_WIDTH": "1920",
    "OSW_SCREEN_HEIGHT": "1080",
}


def protocol_env(arm, environ):
    """The environment the campaign runs under, for `arm`, given the caller's `environ`."""
    if arm == "sonnet":
        return {**_COMMON_ENV, "OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
                "OSW_MAX_OUTPUT_TOKENS": "128000", "OSW_SYSTEM_SUFFIX": "protocol361"}
    if arm == "astra":
        effort = (environ.get("OSW_ASTRA_REASONING_EFFORT") or "").strip()
        if not effort:
            raise SystemExit("ARM=astra: set OSW_ASTRA_REASONING_EFFORT explicitly (the highest "
                             "level Codex accepts for gpt-6-astra)")
        return {**_COMMON_ENV, "OSW_ASTRA_MODEL": "gpt-6-astra",
                "OSW_ASTRA_REASONING_EFFORT": effort,
                "OSW_ASTRA_CAMPAIGN_LOCK": "astra_official361_lock.json",
                "OSW_ASTRA_SYSTEM_SUFFIX": "protocol361"}
    raise SystemExit(f"ARM={arm!r}: expected 'sonnet' or 'astra'")


def batches(task_ids, size):
    return [task_ids[i:i + size] for i in range(0, len(task_ids), size)]


def pending_units(system, runs):
    """Every (task_id, run_idx) of the population without an eval.json, in population order."""
    from benchmarks.osworld import config, tasks
    from core import results
    return [(t["id"], k) for t in tasks.load_tasks() for k in range(1, runs + 1)
            if not results.is_done(results.run_dir(config.RESULTS_DIR, system, t["id"], k))]


def _task_ids(units):
    return list(dict.fromkeys(tid for tid, _ in units))


def _infra_history(system, task_id, run_idx):
    """Outcomes of every infra_error.json record of one run dir, oldest first."""
    from benchmarks.osworld import config
    from core import results
    path = results.run_dir(config.RESULTS_DIR, system, task_id, run_idx) / "infra_error.json"
    if not path.exists():
        return []
    try:
        return [r.get("outcome") for r in json.loads(path.read_text())]
    except Exception:
        return []


def stuck_units(batch_results):
    """Units with >= MAX_NON_QUOTA_INFRA_ERRORS infra records that are not RATE_LIMITED."""
    return [(u["task_id"], u["run_idx"]) for b in batch_results for u in b["units"]
            if sum(o != "RATE_LIMITED" for o in u["infra_history"]) >= MAX_NON_QUOTA_INFRA_ERRORS]


def _silent_failures(batch_results):
    """Batches whose child exited non-zero without writing any infra_error.json."""
    return [b for b in batch_results
            if b["returncode"] != 0 and not any(u["fresh_outcome"] for u in b["units"])]


def decide(batch_results):
    """"stop" | "backoff" | "continue" after one round (see the module docstring)."""
    if _silent_failures(batch_results) or stuck_units(batch_results):
        return "stop"
    attempted = [u for b in batch_results for u in b["units"]]
    limited = sum(u["fresh_outcome"] == "RATE_LIMITED" for u in attempted)
    if attempted and limited / len(attempted) >= RATE_LIMIT_THRESHOLD:
        return "backoff"
    return "continue"


def run_round(system, round_batches, pending, runs, log_file):
    """Start one run.py child per batch (all at once), wait for all, and report per batch its
    exit code and, per pending unit, the infra outcome written during this round (if any)."""
    units_of = {tuple(b): [(tid, k) for tid, k in pending if tid in b] for b in round_batches}
    before = {u: len(_infra_history(system, *u)) for us in units_of.values() for u in us}
    procs = []
    for b in round_batches:
        cmd = [sys.executable, "-m", "benchmarks.osworld.run", "--system", system,
               "--runs", str(runs), "--concurrency", "1", "--ids", *b]
        procs.append(subprocess.Popen(cmd, cwd=_ROOT, stdout=log_file,
                                      stderr=subprocess.STDOUT if log_file else None))
    out = []
    for b, proc in zip(round_batches, procs):
        rc = proc.wait()
        units = []
        for tid, k in units_of[tuple(b)]:
            hist = _infra_history(system, tid, k)
            fresh = hist[-1] if len(hist) > before[(tid, k)] else None
            units.append({"task_id": tid, "run_idx": k, "fresh_outcome": fresh,
                          "infra_history": hist})
        out.append({"returncode": rc, "units": units})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned batches and exit without running anything")
    args = ap.parse_args(argv)

    if "benchmarks.osworld.config" in sys.modules:
        raise SystemExit("benchmarks.osworld.config was imported before the protocol "
                         "environment was exported; it would ignore it")
    arm = os.environ.get("ARM", "").strip()
    os.environ.update(protocol_env(arm, os.environ))
    parallel = int(os.environ.get("PARALLEL", "1"))
    max_hours = float(os.environ.get("MAX_HOURS", "48"))
    if parallel < 1:
        raise SystemExit(f"PARALLEL={parallel}: expected >= 1")

    from benchmarks.osworld import config
    system = config.SYSTEM_NAME if arm == "sonnet" else config.ASTRA_SYSTEM_NAME

    if args.dry_run:
        pending = pending_units(system, RUNS)
        print(json.dumps({"arm": arm, "system": system, "parallel": parallel, "runs": RUNS,
                          "pending_units": len(pending),
                          "batches": batches(_task_ids(pending), BATCH_TASKS)}, indent=2))
        return 0

    log_path = _ROOT / "scripts" / f"g_official361_{arm}.log"
    log_file = open(log_path, "a", buffering=1)

    def log(msg):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    log(f"=== start ARM={arm} SYSTEM={system} PARALLEL={parallel} runs={RUNS} "
        f"max_hours={max_hours} ===")
    from benchmarks.osworld.benchmark import build
    from core.dotenv import load_dotenv
    load_dotenv()   # same as core.run.main, so the in-process preflight sees what children see
    try:
        build().runners[system].preflight()
    except SystemExit as e:
        log(f"preflight failed: {e}")
        return 2
    log("preflight ok")

    deadline = time.time() + max_hours * 3600
    rnd = 0
    while True:
        pending = pending_units(system, RUNS)
        if not pending:
            log("=== done: no pending units ===")
            return 0
        if time.time() >= deadline:
            log(f"=== MAX_HOURS={max_hours} reached: {len(pending)} unit(s) still pending ===")
            return 0
        rnd += 1
        plan = batches(_task_ids(pending), BATCH_TASKS)[:parallel]
        log(f"[round {rnd}] {len(pending)} pending unit(s); starting {len(plan)} batch(es): "
            + " | ".join(" ".join(b) for b in plan))
        results = run_round(system, plan, pending, RUNS, log_file)
        decision = decide(results)
        fresh = Counter(u["fresh_outcome"] for b in results for u in b["units"]
                        if u["fresh_outcome"])
        log(f"[round {rnd}] exit codes {[b['returncode'] for b in results]}; fresh infra "
            f"outcomes {dict(sorted(fresh.items()))}; decision {decision}")
        if decision == "stop":
            for b in _silent_failures(results):
                ids = sorted({u["task_id"] for u in b["units"]})
                log(f"STOP: run.py exited {b['returncode']} without writing infra_error.json "
                    f"(tasks {' '.join(ids)})")
            stuck = stuck_units(results)
            if stuck:
                log(f"STOP: >= {MAX_NON_QUOTA_INFRA_ERRORS} non-RATE_LIMITED infra errors on "
                    + " ".join(f"{tid}#{k}" for tid, k in stuck))
            return 3
        if decision == "backoff":
            log(f"[round {rnd}] >= {RATE_LIMIT_THRESHOLD:.0%} of attempted units RATE_LIMITED -- "
                f"backing off {BACKOFF_S}s")
            time.sleep(BACKOFF_S)


if __name__ == "__main__":
    sys.exit(main())
