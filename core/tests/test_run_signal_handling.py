"""Subprocess tests for core/run.py's SIGTERM/SIGINT reaping of orphaned agent process groups:
  python -m core.tests.test_run_signal_handling

Run as a real subprocess (not in-process) because installing a SIGTERM handler and then
signalling ourselves would be a hazard to the test runner itself.
"""
import os
import signal
import subprocess
import sys
import time

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


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
