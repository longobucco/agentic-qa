"""Local watchdog for the OSWorld G3-full campaign (scope widened 2026-08-16 to all 260 runnable
tasks -- see _pending_task_ids).

Run standalone (no Claude session, no cloud agent) by a local launchd agent every ~2h. If the
overnight driver (`g3_full_overnight_driver.sh`) is not alive, relaunches it -- so the
multi-day campaign keeps making progress even when the Claude subscription's 5-hour session
cap makes a run exhaust its unit list and exit, and nobody is around to notice and manually
resume it. The driver itself loops all night on its own (see that script); this watchdog is
the backstop for the driver process dying outright (crash, unhandled error), not the primary
resume mechanism for routine session-limit rate-limiting -- the driver already re-attempts
those without waiting for a watchdog cycle.

Every invocation (relaunch or no-op) also classifies EVERY not-done (task, run) unit by why
it's pending -- rate-limited (agent_api_error_status set, no eval.json), an infra failure
(infra_error.json's last outcome), or never attempted at all -- and logs the breakdown. That
turns `scripts/g3_full_watchdog.log` into a self-service status report: `--status` runs the
same classification without touching the process, so "how much is left and why" is one
command instead of manually grepping a multi-day run log.

`--print-order` prints the current run order, one task id per line: every remaining task with
a pending rate-limited attempt FIRST (earliest rate-limit timestamp first -- read from each
pending run's own result.json, independent of infra_error.json's single "last outcome" field,
which a later unrelated failure can overwrite), then everything else remaining. The overnight
driver calls this fresh at the start of every pass, so newly rate-limited units jump the queue
on the very next lap instead of waiting to cycle back around alphabetically.

Setup: loaded as a launchd LaunchAgent (see com.agentic-qa.g3-full-watchdog.plist in
~/Library/LaunchAgents) -- plain `crontab` hangs non-interactively on this machine (macOS TCC
permission prompt), launchd doesn't.

Usage:
  python3 scripts/g3_full_watchdog.py               # normal watchdog pass (relaunch if dead)
  python3 scripts/g3_full_watchdog.py --status       # read-only: print the pending breakdown
  python3 scripts/g3_full_watchdog.py --print-order  # read-only: print the run order, 1 id/line
"""
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG = Path(__file__).resolve().parent / "g3_full_watchdog.log"
DRIVER = REPO_ROOT / "scripts" / "g3_full_overnight_driver.sh"
DRIVER_LOG = REPO_ROOT / "scripts" / "g3_full_overnight_driver.log"

# Observed live (2026-08-14): the process can be technically alive (pgrep finds it) while making
# ZERO progress -- a `claude` subprocess hung past its own TASK_TIMEOUT because macOS suspended
# the process during a sleep/wake cycle, which also froze the network connection AND (it looks
# like) the internal timeout clock, so the SIGKILL that should have fired at 45min never did --
# one unit sat stuck for 4h42m while "alive" the whole time. A pgrep-only check is blind to this.
# So: alive is necessary but not sufficient -- the driver log must also have grown recently, or
# we treat it as stuck and force a fresh start instead of trusting "the process exists".
#
# Threshold must clear the legitimate worst case, not just the typical one: one task invocation
# covers up to 3 runs, each individually bounded at ~65min (provision 10min + agent 45min + post-
# run capture/score 10min -- see sandbox.py / agent_computer.py), and core.run only logs once a
# unit *completes* -- no output at all during a single in-progress run. 3 * 65min = 195min. The
# driver's own gtimeout wrapper (see g3_full_overnight_driver.sh) is the PRIMARY defense at a
# matching ceiling; this is the backstop for if that ALSO somehow fails to fire, so it's set
# comfortably above it, not tight against the typical case.
_STALE_LOG_S = 225 * 60   # 225min

# 2026-08-22: manually excluded from the campaign. Each failed provisioning identically --
# "sandbox provisioning/setup exceeded 600s" -- 3 to 6 times in a row, on a fresh Daytona VM
# every single retry. That rules out ordinary per-VM flakiness (which would look randomly
# distributed across tasks, not repeatedly hit the exact same wall on the exact same task while
# 300+ others provision fine); blind retrying wasn't converging and was just burning campaign
# time/rate-limit budget. Needs a real fix (bump OSW_PROVISION_TIMEOUT for these, or find what
# their setup has in common) before re-enabling -- remove an id here once that's done.
#
# 2026-08-23: expanded from 7 to 34 ids -- the same "provisioning exceeded 600s" symptom showed
# up on 27 more tasks overnight, all 3/3 runs each, on the very FIRST attempt (3 independent
# fresh VMs failing identically on try #1, not needing repeated retries to notice like the
# original 7 did) -- same task-specific-setup signature, just caught earlier this time.
_SKIP_TASK_IDS = {
    "0d8b7de3-e8de-4d86-b9fd-dd2dce58a217",
    "121ba48f-9e17-48ce-9bc6-a4fb17a7ebba",
    "236833a3-5704-47fc-888c-4f298f09f799",
    "06fe7178-4491-4589-810f-2e2bc9502122",
    "2888b4e6-5b47-4b57-8bf5-c73827890774",
    "35253b65-1c19-4304-8aa4-6884b8218fc0",
    "368d9ba4-203c-40c1-9fa3-da2f1430ce63",
    "0e5303d4-8820-42f6-b18d-daf7e633de21",
    "7b6c7e24-c58a-49fc-a5bb-d57b80e5b4c3",
    "7f52cab9-535c-4835-ac8c-391ee64dc930",
    "82279c77-8fc6-46f6-9622-3ba96f61b477",
    "82bc8d6a-36eb-4d2d-8801-ef714fb1e55a",
    "9f3f70fc-5afc-4958-a7b7-3bb4fcb01805",
    "9f935cce-0a9f-435f-8007-817732bfc0a5",
    "a728a36e-8bf1-4bb6-9a03-ef039a5233f0",
    "a96b564e-dbe9-42c3-9ccf-b4498073438a",
    "b070486d-e161-459b-aa2b-ef442d973b92",
    "b4f95342-463e-4179-8c3f-193cd7241fb2",
    "b7895e80-f4d1-4648-bee0-4eb45a6f1fa8",
    "c1fa57f3-c3db-4596-8f09-020701085416",
    "cabb3bae-cccb-41bd-9f5d-0f3a9fecd825",
    "da46d875-6b82-4681-9284-653b0c7ae241",
    "da922383-bfa4-4cd3-bbad-6bebab3d7742",
    "dd60633f-2c72-42ba-8547-6f2c8cb0fdb0",
    "df67aebb-fb3a-44fd-b75b-51b6012df509",
    "e135df7c-7687-4ac0-a5f0-76b74438b53e",
    "e1e75309-3ddb-4d09-92ec-de869c928143",
    "e2392362-125e-4f76-a2ee-524b183a3412",
    "f0b971a1-6831-4b9b-a50e-22a6e47f45ba",
    "f3b19d1e-2d48-44e9-b4e1-defcae1a0197",
    "f5d96daf-83a8-4c86-9686-bada31fc66ab",
    "f79439ad-3ee8-4f99-a518-0eb60e5652b0",
    "f8cfa149-d1c1-4215-8dac-4a0932bad3c2",
    "fc6d8143-9452-4171-9459-7f515143419a",
}


def _log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}  {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


def _process_running():
    # match the driver script itself, not the individual `benchmarks.osworld.run` subprocess it
    # launches per task -- that command briefly disappears between tasks, which would otherwise
    # look like "not alive" to a watchdog cycle landing in that gap.
    r = subprocess.run(["pgrep", "-f", "g3_full_overnight_driver.sh"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _force_kill_stuck():
    for pattern in ("g3_full_overnight_driver.sh", "benchmarks.osworld.run",
                     "claude -p You are an autonomous agent"):
        subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)


def _process_alive():
    """Alive means running AND making progress -- see _STALE_LOG_S. A stuck-but-running driver
    is force-killed here so the caller's next relaunch starts from a truly clean state instead
    of racing a zombie that might still hold the sandbox/claude subprocess."""
    if not _process_running():
        return False
    if not DRIVER_LOG.exists():
        return True   # just started, hasn't had time to write yet
    age = time.time() - DRIVER_LOG.stat().st_mtime
    if age <= _STALE_LOG_S:
        return True
    _log(f"driver log stale for {age/60:.0f}min (running but stuck) -- force-killing before relaunch")
    _force_kill_stuck()
    time.sleep(2)
    return False


def _pending_task_ids():
    """Population is now every runnable OSWorld task (260, the 8-supported-app scope), not just
    g3_full_sample's 196-task 'eligible' subset -- 2026-08-16 decision to also attempt the 64
    tasks g3_sample.py excludes as permanently unscorable (broken Chrome setup port, unroutable
    VLC getter, missing postconfig init, broken vm_command_line), even though most of them are
    expected to end in EVAL_ERROR/ENVIRONMENT_ERROR -- attempted-and-inconclusive is preferred
    over never-attempted for full benchmark coverage."""
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.osworld import tasks as tasks_mod
    from benchmarks.osworld import config
    from core import results as results_io

    base = config.RESULTS_DIR / "agent_computer"
    ids = []
    for t in tasks_mod.load_tasks():
        tid = t["id"]
        if tid in _SKIP_TASK_IDS:
            continue
        if any(not results_io.is_done(base / tid / f"run_{n}") for n in (1, 2, 3)):
            ids.append(tid)
    return base, sorted(ids)


def _pending_units():
    """Every not-done (task_id, run_idx) unit in the G3-full population, classified by why."""
    sys.path.insert(0, str(REPO_ROOT))
    from core import results as results_io

    base, tids = _pending_task_ids()
    units = []
    for tid in tids:
        for n in (1, 2, 3):
            out = base / tid / f"run_{n}"
            if results_io.is_done(out):
                continue
            units.append((tid, n, _classify(out)))
    return units


def _classify(out):
    """Best-effort reason a unit has no eval.json yet -- read from whatever it DID leave
    behind (infra_error.json, a rate-limited result.json, or nothing at all)."""
    infra = out / "infra_error.json"
    if infra.exists():
        try:
            attempts = json.loads(infra.read_text())
            return attempts[-1].get("outcome", "infra_error") if attempts else "infra_error"
        except Exception:
            return "infra_error(unreadable)"
    result = out / "result.json"
    if result.exists():
        try:
            r = json.loads(result.read_text())
            if r.get("agent_api_error_status"):
                return "rate_limited"
        except Exception:
            pass
        return "result_without_eval"
    return "never_attempted"


def _earliest_rate_limit_ts(base, tid):
    """Earliest rate-limit timestamp across tid's still-pending runs, read from each run's own
    result.json (agent_api_error_status) -- NOT from infra_error.json's last-outcome field,
    which a later, unrelated failure (e.g. a manually-interrupted retry) can overwrite even
    though the run WAS originally rate-limited."""
    ts = None
    for n in (1, 2, 3):
        rf = base / tid / f"run_{n}" / "result.json"
        if not rf.exists():
            continue
        try:
            d = json.loads(rf.read_text())
        except Exception:
            continue
        if not d.get("agent_api_error_status"):
            continue
        t = d.get("provenance", {}).get("finished_at") or d.get("provenance", {}).get("started_at")
        if t and (ts is None or t < ts):
            ts = t
    return ts


def _n_done(base, tid):
    sys.path.insert(0, str(REPO_ROOT))
    from core import results as results_io
    return sum(1 for n in (1, 2, 3) if results_io.is_done(base / tid / f"run_{n}"))


def ordered_remaining_ids():
    """Remaining task ids, prioritized 2026-08-17 per explicit direction: every PARTIAL task
    (1-2/3 runs already have a real verdict) before any task that's never gotten one, so the 18
    partials in flight finish before the 108 never-attempted tasks start eating fresh rate-limit
    budget. Within each of those two groups, rate-limited-pending ones go first (earliest
    rate-limit timestamp), same rationale as before -- an attempt already spent should retry
    before a fresh one is started. Recomputed fresh on every call so a long-running driver
    re-prioritizes on every pass instead of freezing the order at campaign start."""
    base, pending = _pending_task_ids()
    rl_ts = {tid: t for tid in pending if (t := _earliest_rate_limit_ts(base, tid))}

    def _rate_limited_first(ids):
        ids = set(ids)
        rl_first = [tid for tid, _ in sorted(rl_ts.items(), key=lambda kv: kv[1]) if tid in ids]
        rest = sorted(ids - set(rl_first))
        return rl_first + rest

    partial = [tid for tid in pending if 0 < _n_done(base, tid) < 3]
    never_started = [tid for tid in pending if _n_done(base, tid) == 0]
    return _rate_limited_first(partial) + _rate_limited_first(never_started)


def _breakdown_line(units):
    if not units:
        return "0 pending -- G3-full is complete"
    counts = Counter(reason for _, _, reason in units)
    n_tasks = len({tid for tid, _, _ in units})
    parts = ", ".join(f"{v} {k}" for k, v in counts.most_common())
    return f"{len(units)} run(s) pending across {n_tasks} task(s): {parts}"


def main():
    if "--status" in sys.argv:
        print(_breakdown_line(_pending_units()))
        return

    if "--print-order" in sys.argv:
        for tid in ordered_remaining_ids():
            print(tid)
        return

    units = _pending_units()
    breakdown = _breakdown_line(units)

    if _process_alive():
        _log(f"campaign process alive, no-op -- {breakdown}")
        return

    if not units:
        _log(f"campaign process not alive -- {breakdown}, nothing to relaunch")
        return

    with DRIVER_LOG.open("a") as f:
        p = subprocess.Popen(["/opt/homebrew/bin/bash", str(DRIVER)], stdout=f,
                              stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), start_new_session=True)
    _log(f"campaign process was NOT alive -- relaunched driver pid {p.pid}, "
         f"logging to {DRIVER_LOG} -- {breakdown}")


if __name__ == "__main__":
    main()
