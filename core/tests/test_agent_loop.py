"""Unit tests for core/agent_loop.py's subprocess handling:
  python -m core.tests.test_agent_loop
"""
import os
import sys
import threading
import time

from core import procgroups
from core.agent_loop import _run_raw, extract_answer

_GRANDCHILD_HANG_S = 15   # worst-case regression runtime if the process-group kill breaks


def test_extract_answer_takes_the_last_match():
    text = "blah\nANSWER: first\nmore\nANSWER: second "
    assert extract_answer(text) == "second"


def test_extract_answer_empty_without_a_match():
    assert extract_answer("no answer line here") == ""


def test_run_raw_returns_stdout_on_normal_exit():
    out = _run_raw([sys.executable, "-c", "print('hello')"], timeout=10)
    assert out.strip() == "hello"


def test_run_raw_kills_the_whole_process_group_on_timeout():
    """Regression for a real bug: `claude -p` spawns an MCP server as a grandchild that
    inherits the stdout pipe. A plain `subprocess.run(..., timeout=...)` only kills the
    DIRECT child on TimeoutExpired -- the orphaned grandchild keeps the pipe open and
    `communicate()` blocks forever waiting for EOF that never comes. Reproduced here with a
    surrogate parent that spawns a long-sleeping grandchild inheriting its stdout, then hangs
    itself: with the fix (start_new_session + killpg), _run_raw must return promptly once its
    own `timeout` elapses, not wait out the grandchild's sleep."""
    parent_script = (
        "import subprocess, sys, time; "
        f"gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_GRANDCHILD_HANG_S})']); "
        "sys.stdout.flush(); "
        f"time.sleep({_GRANDCHILD_HANG_S})"
    )
    t0 = time.time()
    _run_raw([sys.executable, "-c", parent_script], timeout=2)
    elapsed = time.time() - t0
    assert elapsed < _GRANDCHILD_HANG_S, (
        f"_run_raw took {elapsed:.1f}s for a timeout=2 call -- the grandchild's stdout pipe "
        f"was not closed, so communicate() waited out its {_GRANDCHILD_HANG_S}s sleep instead "
        f"of returning shortly after the timeout"
    )


def test_run_raw_registers_its_process_group_while_running_and_unregisters_after():
    """core.procgroups is how core.run reaps orphaned agent CLIs on SIGTERM/SIGINT/exception --
    _run_raw must register the child's pgid as soon as it's spawned, and unregister it once
    Popen actually exits, on every path (this test covers the normal-exit path)."""
    seen = {}

    def watch():
        # poll for the registration to appear while the child sleeps
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with procgroups._lock:
                if procgroups._pgids:
                    seen["pgids"] = set(procgroups._pgids)
                    return
            time.sleep(0.02)

    before = set()
    with procgroups._lock:
        before = set(procgroups._pgids)
    t = threading.Thread(target=watch)
    t.start()
    _run_raw([sys.executable, "-c", "import time; time.sleep(0.5)"], timeout=10)
    t.join(timeout=5)

    assert seen.get("pgids", set()) - before, "no new pgid was registered while the child ran"
    with procgroups._lock:
        after = set(procgroups._pgids)
    assert after == before, f"pgid left registered after normal exit: {after - before}"


def test_run_raw_register_race_kills_the_child_and_raises_interrupted():
    """task-10b fix round 2: if the harness is interrupted in the tiny window between
    _run_raw's pre-spawn is_interrupted() check and procgroups.register(pgid), the signal
    handler's kill_all() already ran and NEVER SAW this pgid (it wasn't registered yet) -- the
    child would otherwise be left running, orphaned, forever. _run_raw must re-check right
    after registering and killpg it itself if that race happened; the existing post-reap check
    then still turns this into procgroups.Interrupted."""
    real_register = procgroups.register

    def racy_register(pgid):
        # simulates the harness being interrupted in exactly this window: register, THEN the
        # flag flips (as if the signal handler's mark_interrupted()+kill_all() ran right here).
        real_register(pgid)
        procgroups.mark_interrupted()

    procgroups.register = racy_register
    try:
        t0 = time.monotonic()
        raised = False
        try:
            _run_raw([sys.executable, "-c", "import time; time.sleep(60)"], timeout=10)
        except procgroups.Interrupted:
            raised = True
        elapsed = time.monotonic() - t0
        assert raised, "register-race did not raise procgroups.Interrupted"
        assert elapsed < 5, (
            f"_run_raw took {elapsed:.1f}s -- the racily-registered child was not actually "
            f"killed, so communicate() waited instead of returning promptly"
        )
    finally:
        procgroups.register = real_register
        procgroups._reset_for_tests()


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
