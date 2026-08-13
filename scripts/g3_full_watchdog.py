"""Local watchdog for the OSWorld G3-full campaign.

Run standalone (no Claude session, no cloud agent) by a local launchd agent every ~2h. If the
`benchmarks.osworld.run` process is not alive, recomputes the remaining G3-full task ids and
relaunches it -- so the multi-day campaign keeps making progress even when the Claude
subscription's 5-hour session cap makes a run exhaust its unit list and exit, and nobody is
around to notice and manually resume it.

Every invocation (relaunch or no-op) also classifies EVERY not-done (task, run) unit by why
it's pending -- rate-limited (agent_api_error_status set, no eval.json), an infra failure
(infra_error.json's last outcome), or never attempted at all -- and logs the breakdown. That
turns `scripts/g3_full_watchdog.log` into a self-service status report: `--status` runs the
same classification without touching the process, so "how much is left and why" is one
command instead of manually grepping a multi-day run log.

Setup: loaded as a launchd LaunchAgent (see com.agentic-qa.g3-full-watchdog.plist in
~/Library/LaunchAgents) -- plain `crontab` hangs non-interactively on this machine (macOS TCC
permission prompt), launchd doesn't.

Usage:
  python3 scripts/g3_full_watchdog.py             # normal watchdog pass (relaunch if dead)
  python3 scripts/g3_full_watchdog.py --status     # read-only: print the pending breakdown
"""
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG = Path(__file__).resolve().parent / "g3_full_watchdog.log"


def _log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}  {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


def _process_alive():
    r = subprocess.run(["pgrep", "-f", "benchmarks.osworld.run"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _pending_units():
    """Every not-done (task_id, run_idx) unit in the G3-full population, classified by why."""
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.osworld.analysis.g3_full_sample import remaining
    from benchmarks.osworld import config
    from core import results as results_io

    base = config.RESULTS_DIR / "agent_computer"
    units = []
    for tid in remaining():
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

    units = _pending_units()
    breakdown = _breakdown_line(units)

    if _process_alive():
        _log(f"campaign process alive, no-op -- {breakdown}")
        return

    if not units:
        _log(f"campaign process not alive -- {breakdown}, nothing to relaunch")
        return

    ids = sorted({tid for tid, _, _ in units})
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = REPO_ROOT / "scripts" / f"g3_full_run_{ts}.log"
    cmd = [sys.executable, "-m", "benchmarks.osworld.run", "--system", "agent_computer",
           "--ids", *ids, "--runs", "3"]
    with log_path.open("w") as f:
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
                              start_new_session=True)
    _log(f"campaign process was NOT alive -- relaunched pid {p.pid} on {len(ids)} task(s), "
         f"logging to {log_path} -- {breakdown}")


if __name__ == "__main__":
    main()
