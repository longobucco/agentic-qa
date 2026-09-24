"""Subprocess tests for core/run.py's SIGTERM/SIGINT reaping of orphaned agent process groups:
  python -m core.tests.test_run_signal_handling

Run as a real subprocess (not in-process) because installing a SIGTERM handler and then
signalling ourselves would be a hazard to the test runner itself.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SCRIPT = """
import os, signal, subprocess, sys, time
from core import procgroups, run

run._install_signal_handlers()

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          start_new_session=True)
pgid = os.getpgid(child.pid)
procgroups.register(pgid)
print(pgid, flush=True)
time.sleep(60)
"""


def _alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS: signalling a zombie's pgid (already dead, not yet reaped) raises
        # PermissionError rather than ProcessLookupError -- treat both as "gone".
        return False


def test_sigterm_kills_registered_groups_and_exits_128_plus_signum():
    proc = subprocess.Popen([sys.executable, "-c", _SCRIPT], stdout=subprocess.PIPE, text=True,
                            cwd=os.getcwd())
    try:
        line = proc.stdout.readline()
        pgid = int(line.strip())
        time.sleep(0.2)   # let the handler installation + registration settle
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        assert rc == 128 + signal.SIGTERM, f"expected exit {128 + signal.SIGTERM}, got {rc}"
        deadline = time.monotonic() + 5
        while _alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pgid), "SIGTERM handler did not reap the registered process group"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- task-10b fix round 1: a real core.run.main() drives units inside a ThreadPoolExecutor,
# not on the main thread -- the signal handler alone (tested above) is not enough. This drives
# an actual `main()` call with a fake benchmark whose runner spawns a real sleeper through
# `core.agent_loop._run_raw`, and verifies: the queue stops (no un-started unit ever runs),
# the in-flight unit's `_run_raw` raises procgroups.Interrupted instead of returning partial
# output, `work()` records that as an INTERRUPTED infra error (never eval.json), the
# environment context manager's own `finally` still ran, and the process exits 128+15.
_DRIVER_TEMPLATE = '''
import sys
from pathlib import Path
from contextlib import contextmanager

from core.run import Benchmark, Runner, main
from core.judge import Judge
from core.environment import Env
from core.agent_loop import _run_raw

RESULTS_DIR = Path(__RESULTS_DIR__)
MARKER_DIR = Path(__MARKER_DIR__)
TASK_IDS = __TASK_IDS__
CONCURRENCY = __CONCURRENCY__


@contextmanager
def env_cm(task, *, port=None):
    (MARKER_DIR / ("env_entered_" + task["id"])).write_text("1")
    try:
        yield Env(port=None)
    finally:
        (MARKER_DIR / ("env_exited_" + task["id"])).write_text("1")


def run_fn(task, *, env, out, refs=None, dry=False):
    (MARKER_DIR / ("unit_started_" + task["id"])).write_text("1")
    pid_marker = MARKER_DIR / ("pid_" + task["id"])
    code = ("import os,time; open(" + repr(str(pid_marker))
            + ", 'w').write(str(os.getpid())); time.sleep(60)")
    _run_raw([sys.executable, "-c", code], timeout=120)
    return "ANSWER: DONE"


def judge_fn(task, answer, ref, out):
    return {"verdict": "SUCCESS", "reason": "ok"}


benchmark = Benchmark(
    name="fake10b",
    results_dir=RESULTS_DIR,
    load_tasks=lambda: [{"id": tid} for tid in TASK_IDS],
    runners={"fake": Runner(name="fake", run=run_fn, environment=env_cm,
                            concurrency_safe=True)},
    judge=Judge(fn=judge_fn, is_deterministic=True),
)

main(benchmark, argv=["--system", "fake", "--concurrency", str(CONCURRENCY)])
'''


def _run_driver(task_ids, concurrency, wait_for_started):
    """Spawn the driver script as a real subprocess, block until every id in
    `wait_for_started` has written its "started" marker, send SIGTERM, and return
    (returncode, results_dir, marker_dir). Cleans up the subprocess even on failure."""
    results_dir = Path(tempfile.mkdtemp(prefix="osw10b_results_"))
    marker_dir = Path(tempfile.mkdtemp(prefix="osw10b_markers_"))
    script = (_DRIVER_TEMPLATE
              .replace("__RESULTS_DIR__", repr(str(results_dir)))
              .replace("__MARKER_DIR__", repr(str(marker_dir)))
              .replace("__TASK_IDS__", repr(task_ids))
              .replace("__CONCURRENCY__", repr(concurrency)))
    script_path = Path(tempfile.mkdtemp(prefix="osw10b_script_")) / "driver.py"
    script_path.write_text(script)
    proc = subprocess.Popen([sys.executable, str(script_path)], cwd=os.getcwd())
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all((marker_dir / f"unit_started_{tid}").exists() for tid in wait_for_started):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"driver never started {wait_for_started}")
        time.sleep(0.3)   # let _run_raw's Popen + procgroups.register settle before signalling
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return rc, results_dir, marker_dir


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False   # macOS: a zombie's pid can raise PermissionError -- treat as gone


def _wait_until_dead(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_main_sigterm_at_concurrency_1_stops_the_queue_and_records_interrupted():
    rc, results_dir, marker_dir = _run_driver(["t1", "t2"], concurrency=1,
                                              wait_for_started=["t1"])
    assert rc == 128 + signal.SIGTERM, f"expected exit {128 + signal.SIGTERM}, got {rc}"

    # cancel_futures=True: the second unit must never have started at all.
    assert not (marker_dir / "unit_started_t2").exists()
    t2_dir = results_dir / "fake" / "t2"
    assert not t2_dir.exists() or not any(t2_dir.rglob("*.json"))

    # the in-flight unit was interrupted, not scored: infra_error(INTERRUPTED), no eval.json.
    t1_run = results_dir / "fake" / "t1" / "run_1"
    assert not (t1_run / "eval.json").exists()
    infra = json.loads((t1_run / "infra_error.json").read_text())
    assert infra[-1]["outcome"] == "INTERRUPTED"

    # the environment context manager's own `finally` ran despite the interrupt.
    assert (marker_dir / "env_exited_t1").exists()

    # the sleeper's process is actually gone (not left running past the harness).
    pid = int((marker_dir / "pid_t1").read_text())
    assert _wait_until_dead(pid), "SIGTERM did not reap the in-flight unit's agent process"


def test_main_sigterm_at_concurrency_2_reaps_both_in_flight_units():
    rc, results_dir, marker_dir = _run_driver(["t1", "t2", "t3"], concurrency=2,
                                              wait_for_started=["t1", "t2"])
    assert rc == 128 + signal.SIGTERM, f"expected exit {128 + signal.SIGTERM}, got {rc}"

    # the third unit (queued behind the 2 workers) must never have started.
    assert not (marker_dir / "unit_started_t3").exists()
    t3_dir = results_dir / "fake" / "t3"
    assert not t3_dir.exists() or not any(t3_dir.rglob("*.json"))

    for tid in ("t1", "t2"):
        run_dir = results_dir / "fake" / tid / "run_1"
        assert not (run_dir / "eval.json").exists()
        infra = json.loads((run_dir / "infra_error.json").read_text())
        assert infra[-1]["outcome"] == "INTERRUPTED"
        assert (marker_dir / f"env_exited_{tid}").exists()
        pid = int((marker_dir / f"pid_{tid}").read_text())
        assert _wait_until_dead(pid), f"SIGTERM did not reap {tid}'s agent process"


# --- task-10b fix round 2: an in-flight worker notices the interrupt only at its NEXT
# spawner call. Under the official protocol, common.protocol_wait(POST_SETUP_WAIT_S=60) sits
# between the agent call and the next spawner call -- if it just blindly sleeps, an interrupt
# that lands while a worker is inside that wait isn't noticed until the sleep finishes, which
# can dwarf core.run's shutdown(wait=True) grace period. This drives a real main() whose fake
# runner calls the REAL common.protocol_wait(30), and verifies a SIGTERM sent mid-wait is
# noticed at once (exit well under 30s), not waited out.
_PROTOCOL_WAIT_DRIVER_TEMPLATE = '''
from pathlib import Path
from contextlib import contextmanager

from core.run import Benchmark, Runner, main
from core.judge import Judge
from core.environment import Env
from benchmarks.osworld.runners.common import protocol_wait

RESULTS_DIR = Path(__RESULTS_DIR__)
MARKER_DIR = Path(__MARKER_DIR__)
TASK_IDS = __TASK_IDS__


@contextmanager
def env_cm(task, *, port=None):
    (MARKER_DIR / ("env_entered_" + task["id"])).write_text("1")
    try:
        yield Env(port=None)
    finally:
        (MARKER_DIR / ("env_exited_" + task["id"])).write_text("1")


def run_fn(task, *, env, out, refs=None, dry=False):
    (MARKER_DIR / ("unit_started_" + task["id"])).write_text("1")
    protocol_wait(30)   # upstream-style settle sleep -- must be interrupted, not waited out
    return "ANSWER: DONE"


def judge_fn(task, answer, ref, out):
    return {"verdict": "SUCCESS", "reason": "ok"}


benchmark = Benchmark(
    name="fake10b",
    results_dir=RESULTS_DIR,
    load_tasks=lambda: [{"id": tid} for tid in TASK_IDS],
    runners={"fake": Runner(name="fake", run=run_fn, environment=env_cm,
                            concurrency_safe=True)},
    judge=Judge(fn=judge_fn, is_deterministic=True),
)

main(benchmark, argv=["--system", "fake", "--concurrency", "1"])
'''


def _run_protocol_wait_driver(task_ids):
    """Same shape as _run_driver, for the protocol_wait-based script. Returns (returncode,
    elapsed_seconds, results_dir, marker_dir)."""
    results_dir = Path(tempfile.mkdtemp(prefix="osw10b_pw_results_"))
    marker_dir = Path(tempfile.mkdtemp(prefix="osw10b_pw_markers_"))
    script = (_PROTOCOL_WAIT_DRIVER_TEMPLATE
              .replace("__RESULTS_DIR__", repr(str(results_dir)))
              .replace("__MARKER_DIR__", repr(str(marker_dir)))
              .replace("__TASK_IDS__", repr(task_ids)))
    script_path = Path(tempfile.mkdtemp(prefix="osw10b_pw_script_")) / "driver.py"
    script_path.write_text(script)
    proc = subprocess.Popen([sys.executable, str(script_path)], cwd=os.getcwd())
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (marker_dir / f"unit_started_{task_ids[0]}").exists():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("driver never started its unit")
        time.sleep(0.3)   # let protocol_wait's Event.wait() actually be entered before signalling
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return rc, elapsed, results_dir, marker_dir


def test_main_sigterm_during_protocol_wait_exits_promptly_and_records_interrupted():
    rc, elapsed, results_dir, marker_dir = _run_protocol_wait_driver(["t1"])
    assert rc == 128 + signal.SIGTERM, f"expected exit {128 + signal.SIGTERM}, got {rc}"
    assert elapsed < 10, (
        f"took {elapsed:.1f}s to exit after SIGTERM during a 30s protocol_wait -- the wait "
        f"was not interrupted, it was waited out"
    )

    t1_run = results_dir / "fake" / "t1" / "run_1"
    assert not (t1_run / "eval.json").exists()
    infra = json.loads((t1_run / "infra_error.json").read_text())
    assert infra[-1]["outcome"] == "INTERRUPTED"
    assert (marker_dir / "env_exited_t1").exists()


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
