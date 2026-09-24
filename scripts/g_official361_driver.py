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

No protocol drift from the caller's shell (or the repo .env, which every child loads): before
exporting anything, the driver refuses (exit 2, naming each variable) when the caller has set a
harness-altering knob (_HARNESS_KNOB_DEFAULTS below) to anything but its default, or one of the
variables it exports to a different value.
The VM image is part of the protocol: OSW_KVM_IMAGE must be a digest reference of the official
image, happysixd/osworld-docker@sha256:<64 hex> (as scripts/kvm_host_setup.sh prints it), or the
driver refuses -- the kvm preflight only checks that the image EXISTS on the docker host, not
which one it is, and the system name (..._official_kvm) would not show a swap. Every knob of
_HARNESS_KNOB_DEFAULTS is then exported at its default (even "") into the children's
environment, so a mid-campaign .env edit cannot inject one through core.run's load_dotenv
(setdefault never overrides a variable that is present). Deliberately not checked: the other
OSW_KVM_* (host settings: address, docker host, qcow2 path/sha256 -- the kvm preflight checks
they are set/present, not their identity), OSW_ASTRA_REASONING_EFFORT (the caller's campaign
decision), and knobs no code path of these two kvm arms reads -- OSW_IMAGE,
OSW_PROVISION_TIMEOUT (Daytona only), OSW_OPENBOOK_* (open-book runner), OSW_VR_*
(verify-replan runner), OSW_RAW_BASE/OSW_INDEX (data download), OSW_PROBE_*.

Stuck units: before each round, a pending unit whose infra_error.json already holds >= 3
non-RATE_LIMITED records (e.g. from an earlier session) stops the driver (exit 3) instead of
costing another VM. A unit attempted in a round that ends with neither eval.json nor a new infra
record (unparsable infra_error.json included) is logged as "no outcome written" and counts as one
non-RATE_LIMITED failure toward the same rule (counted in memory, for this driver process).

Signals: SIGTERM/SIGINT make the driver SIGTERM its running run.py children, wait up to
CHILD_GRACE_S, SIGKILL whatever is left, log it and exit 128 + signum -- no orphaned children.
A child killed that way never reaches kvm_environment's finally (Python's default SIGTERM action
runs none, and the VM lives in a worker thread a KeyboardInterrupt never reaches), so each driver
process exports a unique OSW_KVM_DRIVER_RUN, kvm_environment labels every container with it,
and after terminating the children -- and on every exit, as a safety sweep -- the driver
force-removes the containers on OSW_KVM_DOCKER_HOST carrying exactly its label. Containers with
another or no label (e.g. the other arm's campaign on the same host) are never touched.
OSW_KVM_DRIVER_RUN is internal: always overwritten, never on the refuse list.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import re
import time
import uuid
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
CHILD_GRACE_S = 60

# Every OSW_* knob a run.py child of these arms reads that changes the harness (tools, prompts,
# budgets, timings, scoring, which tasks, which VM), with the value that means "unset". Empty
# string: any non-empty value changes behavior.
_HARNESS_KNOB_DEFAULTS = {
    "OSW_RELEASE": "verified",
    "OSW_ZOOM_BATCH": "0",
    "OSW_GROUNDING": "0",
    "OSW_GROUNDING_VERIFY": "1",
    "OSW_GROUNDING_MIN_SCORE": "0.45",
    "OSW_SELF_VERIFY": "0",
    "OSW_INLOOP_VERIFY": "0",
    "OSW_INLOOP_VERIFY_MAX_TURNS": "40",
    "OSW_INLOOP_VERIFY_SKIP_APPS": "os",
    "OSW_RESTRICT_RUN_PYTHON": "0",
    "OSW_ENFORCE_SANDBOX": "0",
    "OSW_OBSERVATION": "screenshot+a11y",
    "OSW_ACTION_SPACE": "pyautogui",
    "OSW_MAX_TURNS": "150",
    "OSW_TASK_TIMEOUT": "",            # unset -> 14400 under the protocol
    "OSW_SLEEP_AFTER_EXECUTION": "0.5",
    "OSW_POST_SETUP_WAIT_S": "60",
    "OSW_PRE_EVAL_WAIT_S": "20",
    "OSW_POST_RUN_TIMEOUT": "600",
    "OSW_INCLUDE_ALL_APPS": "",
    "OSW_PINNED_EVALUATORS": "1",
    "OSW_CONTROLLER_URL": "",          # reuse an existing desktop instead of a fresh VM
    "OSW_CONTROLLER_PORT": "5000",
    "OSW_SANDBOX_ID": "",
    "OSW_ASTRA_CODEX_VERSION": "0.153.4",
}

# The official VM image, pinned by digest (scripts/kvm_host_setup.sh prints the reference).
_KVM_IMAGE_RE = re.compile(r"happysixd/osworld-docker@sha256:[0-9a-f]{64}")

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


def env_conflicts(arm, environ):
    """`NAME=value (...)` for every caller variable that would make the campaign drift from the
    protocol: a harness knob off its default, or an exported variable set to another value."""
    out = []
    for name, default in _HARNESS_KNOB_DEFAULTS.items():
        value = environ.get(name)
        if value is not None and value.strip() != default:
            out.append(f"{name}={value!r} (the protocol requires it unset or {default!r})")
    for name, required in protocol_env(arm, environ).items():
        value = environ.get(name)
        if value is not None and value.strip() != required:
            out.append(f"{name}={value!r} (the protocol requires {required!r})")
    image = environ.get("OSW_KVM_IMAGE")
    if not _KVM_IMAGE_RE.fullmatch((image or "").strip()):
        out.append(f"OSW_KVM_IMAGE={image!r} (the protocol requires a pinned digest "
                   f"happysixd/osworld-docker@sha256:<64 hex>; scripts/kvm_host_setup.sh "
                   f"prints it)")
    return out


def child_env(arm, environ):
    """What the driver exports for itself and its children: every harness knob pinned at its
    default, plus the protocol environment."""
    return {**_HARNESS_KNOB_DEFAULTS, **protocol_env(arm, environ)}


def terminate_children(procs, grace_s, log):
    """SIGTERM every still-running child, wait up to `grace_s` in total, SIGKILL the rest.
    Returns (signalled, killed)."""
    running = [p for p in procs if p.poll() is None]
    for p in running:
        p.terminate()
    deadline = time.time() + grace_s
    killed = 0
    for p in running:
        try:
            p.wait(timeout=max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            killed += 1
    log(f"sent SIGTERM to {len(running)} running run.py child(ren); {killed} killed after "
        f"{grace_s}s grace")
    return len(running), killed


_DRIVER_RUN_LABEL = "osworld.driver_run"   # == kvm_vm.DRIVER_RUN_LABEL (not imported: config)


def _docker_client():
    from benchmarks.osworld.env import kvm_vm
    return kvm_vm._docker_client()


def sweep_containers(run_id, log, client=None):
    """Force-remove (with volumes) every container labelled osworld.driver_run == `run_id`;
    return how many were removed. The label is re-checked here, not only trusted to the daemon's
    filter: another driver's containers must never be touched. Docker errors are logged, never
    raised (this runs on the way out, possibly under a signal exit code)."""
    if not run_id:
        raise ValueError("empty driver run id: would match every container started outside a "
                         "driver")
    removed = 0
    try:
        client = client or _docker_client()
        found = client.containers.list(all=True,
                                       filters={"label": f"{_DRIVER_RUN_LABEL}={run_id}"})
        for c in found:
            if (c.labels or {}).get(_DRIVER_RUN_LABEL) != run_id:
                continue
            try:
                c.remove(force=True, v=True)
                removed += 1
            except Exception as e:
                log(f"container sweep: could not remove {c.id}: {e!r}")
        log(f"container sweep: removed {removed} container(s) labelled "
            f"{_DRIVER_RUN_LABEL}={run_id}")
    except Exception as e:
        log(f"container sweep failed ({removed} removed): {e!r}")
    return removed


class _Interrupted(Exception):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum, frame):
    raise _Interrupted(signum)


_children = []   # run.py children currently running (terminated on SIGTERM/SIGINT)


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


def _non_quota_failures(history, no_outcomes=0):
    return sum(o != "RATE_LIMITED" for o in history) + no_outcomes


def stuck_units(batch_results):
    """Units with >= MAX_NON_QUOTA_INFRA_ERRORS non-RATE_LIMITED failures: infra records, plus
    the rounds of this driver process that ended with no outcome written at all."""
    return [(u["task_id"], u["run_idx"]) for b in batch_results for u in b["units"]
            if _non_quota_failures(u["infra_history"], u.get("no_outcomes", 0))
            >= MAX_NON_QUOTA_INFRA_ERRORS]


def stuck_pending(system, pending):
    """Pending units already stuck on disk (e.g. from an earlier session), before any VM."""
    return [(tid, k) for tid, k in pending
            if _non_quota_failures(_infra_history(system, tid, k)) >= MAX_NON_QUOTA_INFRA_ERRORS]


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


def run_round(system, round_batches, pending, runs, log_file, no_outcomes=None):
    """Start one run.py child per batch (all at once), wait for all, and report per batch its
    exit code and, per pending unit, the infra outcome written during this round (if any), or
    that it ended with no outcome at all -- counted in `no_outcomes` ({unit: n}, updated), which
    the caller keeps across rounds."""
    from benchmarks.osworld import config
    from core import results
    no_outcomes = {} if no_outcomes is None else no_outcomes
    units_of = {tuple(b): [(tid, k) for tid, k in pending if tid in b] for b in round_batches}
    before = {u: len(_infra_history(system, *u)) for us in units_of.values() for u in us}
    procs = []
    for b in round_batches:
        cmd = [sys.executable, "-m", "benchmarks.osworld.run", "--system", system,
               "--runs", str(runs), "--concurrency", "1", "--ids", *b]
        proc = subprocess.Popen(cmd, cwd=_ROOT, stdout=log_file,
                                stderr=subprocess.STDOUT if log_file else None)
        procs.append(proc)
        _children.append(proc)
    out = []
    for b, proc in zip(round_batches, procs):
        rc = proc.wait()
        _children.remove(proc)
        units = []
        for tid, k in units_of[tuple(b)]:
            hist = _infra_history(system, tid, k)
            fresh = hist[-1] if len(hist) > before[(tid, k)] else None
            none = fresh is None and not results.is_done(
                results.run_dir(config.RESULTS_DIR, system, tid, k))
            if none:
                no_outcomes[(tid, k)] = no_outcomes.get((tid, k), 0) + 1
            units.append({"task_id": tid, "run_idx": k, "fresh_outcome": fresh,
                          "infra_history": hist, "no_outcome": none,
                          "no_outcomes": no_outcomes.get((tid, k), 0)})
        out.append({"returncode": rc, "units": units})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned batches and exit without running anything")
    args = ap.parse_args(argv)

    # The repo-root .env first (setdefault: the shell wins), as core.run.main does in every child:
    # a knob set there reaches the children too, so the drift check below must see it, and the
    # in-process preflight then sees what the children see.
    from core.dotenv import load_dotenv
    load_dotenv()
    arm = os.environ.get("ARM", "").strip()
    conflicts = env_conflicts(arm, os.environ)
    if conflicts:
        print("refusing to start: the caller's environment would change the protocol:\n  "
              + "\n  ".join(conflicts), file=sys.stderr)
        return 2
    if "benchmarks.osworld.config" in sys.modules:
        raise SystemExit("benchmarks.osworld.config was imported before the protocol "
                         "environment was exported; it would ignore it")
    os.environ.update(child_env(arm, os.environ))
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

    run_id = uuid.uuid4().hex
    os.environ["OSW_KVM_DRIVER_RUN"] = run_id   # inherited by every child -> container label
    log(f"=== start ARM={arm} SYSTEM={system} PARALLEL={parallel} runs={RUNS} "
        f"max_hours={max_hours} driver_run={run_id} ===")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _raise_interrupted)
    try:
        return _campaign(system, parallel, max_hours, log, log_file)
    except _Interrupted as e:
        for signum in (signal.SIGTERM, signal.SIGINT):   # a second Ctrl-C must not cut cleanup
            signal.signal(signum, signal.SIG_IGN)
        name = signal.Signals(e.signum).name
        log(f"=== {name} received: stopping the running run.py children ===")
        terminate_children(list(_children), CHILD_GRACE_S, log)
        return 128 + e.signum
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        if _children:   # an unexpected exception mid-round: never sweep under live children
            terminate_children(list(_children), CHILD_GRACE_S, log)
        sweep_containers(run_id, log)


def _campaign(system, parallel, max_hours, log, log_file):
    """Preflight, then rounds until nothing is pending, MAX_HOURS, or a stop decision."""
    from benchmarks.osworld.benchmark import build
    try:
        build().runners[system].preflight()
    except SystemExit as e:
        log(f"preflight failed: {e}")
        return 2
    log("preflight ok")

    deadline = time.time() + max_hours * 3600
    rnd = 0
    no_outcomes = {}   # unit -> rounds of this process that ended with no outcome written
    while True:
        pending = pending_units(system, RUNS)
        if not pending:
            log("=== done: no pending units ===")
            return 0
        if time.time() >= deadline:
            log(f"=== MAX_HOURS={max_hours} reached: {len(pending)} unit(s) still pending ===")
            return 0
        stuck = stuck_pending(system, pending)
        if stuck:
            log(f"STOP: >= {MAX_NON_QUOTA_INFRA_ERRORS} non-RATE_LIMITED infra errors already on "
                "disk (not spending another VM) on " + " ".join(f"{tid}#{k}" for tid, k in stuck))
            return 3
        rnd += 1
        plan = batches(_task_ids(pending), BATCH_TASKS)[:parallel]
        log(f"[round {rnd}] {len(pending)} pending unit(s); starting {len(plan)} batch(es): "
            + " | ".join(" ".join(b) for b in plan))
        results = run_round(system, plan, pending, RUNS, log_file, no_outcomes)
        decision = decide(results)
        silent = [f"{u['task_id']}#{u['run_idx']}" for b in results for u in b["units"]
                  if u["no_outcome"]]
        if silent:
            log(f"[round {rnd}] no outcome written (neither eval.json nor infra_error.json) for "
                + " ".join(silent))
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
                log(f"STOP: >= {MAX_NON_QUOTA_INFRA_ERRORS} non-RATE_LIMITED failures (infra "
                    f"errors or no outcome written) on "
                    + " ".join(f"{tid}#{k}" for tid, k in stuck))
            return 3
        if decision == "backoff":
            log(f"[round {rnd}] >= {RATE_LIMIT_THRESHOLD:.0%} of attempted units RATE_LIMITED -- "
                f"backing off {BACKOFF_S}s")
            time.sleep(BACKOFF_S)


if __name__ == "__main__":
    sys.exit(main())
