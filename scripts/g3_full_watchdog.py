"""Local watchdog for the OSWorld G3-full campaign.

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
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.osworld.analysis.g3_full_sample import remaining
    from benchmarks.osworld import config
    from core import results as results_io

    base = config.RESULTS_DIR / "agent_computer"
    ids = []
    for tid in remaining():
        if any(not results_io.is_done(base / tid / f"run_{n}") for n in (1, 2, 3)):
            ids.append(tid)
    return base, ids


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


def ordered_remaining_ids():
    """Remaining task ids, rate-limited-pending ones first (chronological), then the rest
    (alphabetical). Recomputed fresh on every call so a long-running driver re-prioritizes on
    every pass instead of freezing the order at campaign start."""
    base, pending = _pending_task_ids()
    rl_ts = {tid: t for tid in pending if (t := _earliest_rate_limit_ts(base, tid))}
    rate_limited_first = [tid for tid, _ in sorted(rl_ts.items(), key=lambda kv: kv[1])]
    rest = sorted(set(pending) - set(rate_limited_first))
    return rate_limited_first + rest


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
