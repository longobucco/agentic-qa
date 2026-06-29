"""Re-run tasks killed by the Claude session/usage limit, throttled to ride under the cap.

The full run hit the subscription's 5-hour session limit mid-way; every task after that just
recorded the CLI's "You've hit your session limit · resets <time>" message instead of running
the agent. This re-runs exactly those tasks at concurrency 1, and when the limit is hit again
it PAUSES until the window resets (polling a tiny probe) rather than burning the window on
dead runs. Resumable + idempotent: a task counts as done only once its transcript shows the
agent actually ran (no limit message), so re-running this picks up wherever it left off.

  python -m benchmarks.webvoyager.drain_rerun           # drain all throttled tasks
"""
import json
import os
import subprocess
import sys
import time

from benchmarks.webvoyager import config, tasks

R = config.RESULTS_DIR / "agent_browser"
LIMIT_MARKERS = ("session limit", "usage limit", "hit your")
CHUNK = 20            # tasks per harness invocation before re-probing the window
PROBE_SLEEP = 600     # wait between probes while throttled (10 min)
STALL_SLEEP = 1200    # extra pause if a chunk made no progress (20 min)


def _throttled_text(t: str) -> bool:
    return any(m in (t or "").lower() for m in LIMIT_MARKERS)


def needs_rerun(tid: str) -> bool:
    """A task must be re-run if the agent never actually ran: no transcript, an empty one, or
    one that is just the session-limit message."""
    ao = R / tid / "run_1" / "agent_output.txt"
    if not ao.exists():
        return True
    txt = ao.read_text(errors="ignore")
    return len(txt.strip()) < 50 or _throttled_text(txt)


def probe_ok() -> bool:
    """True iff claude responds normally right now (not throttled)."""
    try:
        p = subprocess.run(["claude", "-p", "reply OK", "--output-format", "json"],
                           capture_output=True, text=True, timeout=90,
                           stdin=subprocess.DEVNULL)
        res = (json.loads(p.stdout).get("result") or "")
        return bool(res.strip()) and not _throttled_text(res)
    except Exception:
        return False


def main():
    all_ids = [t["id"] for t in tasks.load_tasks()]
    remaining = [t for t in all_ids if needs_rerun(t)]
    print(f"[drain] {len(remaining)} tasks need re-run (throttled in the original run)",
          flush=True)
    env = {**os.environ, "WV_AGENT_DOCKER": "1"}

    while remaining:
        waited = 0
        while not probe_ok():
            print(f"[drain] throttled — window not open yet; waited {waited // 60}m", flush=True)
            time.sleep(PROBE_SLEEP)
            waited += PROBE_SLEEP

        chunk = remaining[:CHUNK]
        print(f"[drain] window OPEN — running {len(chunk)} tasks (remaining {len(remaining)})",
              flush=True)
        subprocess.run([sys.executable, "-m", "benchmarks.webvoyager.run",
                        "--ids", *chunk, "--force", "--concurrency", "1"], env=env)

        before = len(remaining)
        remaining = [t for t in remaining if needs_rerun(t)]
        print(f"[drain] chunk done — remaining {len(remaining)} (was {before})", flush=True)
        if len(remaining) == before:
            print(f"[drain] no progress (limit likely hit mid-chunk); pausing "
                  f"{STALL_SLEEP // 60}m for reset", flush=True)
            time.sleep(STALL_SLEEP)
        else:
            time.sleep(10)   # gentle pacing between chunks

    print("[drain] DRAIN COMPLETE — all throttled tasks re-run", flush=True)


if __name__ == "__main__":
    main()
