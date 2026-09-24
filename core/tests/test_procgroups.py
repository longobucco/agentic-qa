"""Unit tests for core/procgroups.py's process-group registry:
  python -m core.tests.test_procgroups
"""
import os
import signal
import subprocess
import sys
import time

from core import procgroups


def _spawn_group():
    """A real child in its own process group, sleeping long enough to observe a kill."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    return proc, os.getpgid(proc.pid)


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


def test_register_unregister_round_trip():
    pgid = 999999   # never a real pgid; just exercising bookkeeping
    procgroups.register(pgid)
    try:
        with procgroups._lock:
            assert pgid in procgroups._pgids
    finally:
        procgroups.unregister(pgid)
    with procgroups._lock:
        assert pgid not in procgroups._pgids


def test_kill_all_kills_a_real_sleeping_process_group():
    proc, pgid = _spawn_group()
    procgroups.register(pgid)
    try:
        assert _alive(pgid)
        procgroups.kill_all(sig=signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pgid), "kill_all did not reap the registered group"
    finally:
        procgroups.unregister(pgid)
        if proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_kill_all_swallows_process_lookup_error_for_a_dead_group():
    pgid = 999998
    procgroups.register(pgid)
    try:
        procgroups.kill_all(sig=signal.SIGKILL)   # must not raise: no such pgid
    finally:
        procgroups.unregister(pgid)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
