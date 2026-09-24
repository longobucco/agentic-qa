"""Registry of live agent-CLI process groups, shared by every spawner (`core.agent_loop`,
`core.codex_loop`) and reaped by `core.run.main` on interruption.

Both `core.agent_loop._run_raw` and `core.codex_loop.run_codex_meta` start the agent CLI in
its own process group (`start_new_session=True`) and already killpg it on their own timeout.
That leaves one gap: if the *harness* process itself is torn down (SIGTERM from a campaign
driver, SIGINT / Ctrl-C, or an unhandled exception unwinding `core.run.main`), nothing ever
kills the agent CLI's group -- it (and its MCP-server grandchild) is orphaned, keeps running
past the VM's teardown, and burns subscription quota for nothing. This module is the missing
piece: a small, thread-safe set of "groups currently in flight", so a top-level handler can
reap all of them regardless of which spawner started them or which worker thread owns them.
"""
import os
import signal
import threading

_lock = threading.Lock()
_pgids = set()

# Process-wide: set by core.run's signal handler BEFORE it calls kill_all(), so a spawner whose
# group is being killed *because the harness itself is shutting down* can tell that apart from
# its own ordinary timeout and raise Interrupted instead of quietly returning partial output
# that would otherwise get judged and scored as if the run had actually finished.
_interrupted = threading.Event()


class Interrupted(Exception):
    """Raised by a spawner (`core.agent_loop._run_raw`, `core.codex_loop.run_codex_meta`) when
    the agent CLI's process group ended because the harness was interrupted (SIGTERM/SIGINT),
    not because of the spawner's own timeout -- or when the harness was already interrupted
    before the spawner got a chance to start a process at all. Callers must let this propagate
    all the way to `core.run`'s `work()`, which records it as an infra outcome ("INTERRUPTED")
    and never writes eval.json for it -- swallowing it into an ordinary result would silently
    score a run that was actually killed mid-flight."""


def register(pgid):
    """Record `pgid` as a live agent process group."""
    with _lock:
        _pgids.add(pgid)


def unregister(pgid):
    """Stop tracking `pgid` (call once the spawner's own Popen has finished, on every path --
    normal exit, timeout, or exception). Safe to call even if `pgid` was never registered."""
    with _lock:
        _pgids.discard(pgid)


def mark_interrupted():
    """Record that the harness itself has been interrupted (called by core.run's signal handler
    BEFORE kill_all()). Sticky for the life of the process -- there is no `clear()` because a
    harness process that has started shutting down never un-shuts-down."""
    _interrupted.set()


def is_interrupted():
    return _interrupted.is_set()


def wait_interrupted(timeout):
    """Block up to `timeout` seconds, or return as soon as the harness is interrupted --
    whichever comes first. True if it returned because of an interrupt (the caller should
    treat the wait as cut short, not completed), False on a plain timeout. This is what makes
    a long settle sleep (e.g. `benchmarks.osworld.runners.common.protocol_wait`'s
    POST_SETUP_WAIT_S=60) notice a SIGTERM/SIGINT immediately instead of only at the NEXT
    spawner call -- without it, `core.run`'s `ex.shutdown(wait=True, ...)` could block for the
    full 60s, long enough for a campaign driver's own grace period to SIGKILL the whole
    process before an INTERRUPTED infra record is ever written (task-10b fix round 2)."""
    return _interrupted.wait(timeout)


def _reset_for_tests():
    """Test-only: clear the interrupted flag. Production code must NEVER call this -- the flag
    is deliberately sticky (see mark_interrupted). Exists only so an in-process test that
    exercises mark_interrupted()/is_interrupted() doesn't permanently poison every later test
    in the same pytest session; every such test must call this in a `finally`."""
    _interrupted.clear()


def kill_all(sig=signal.SIGKILL):
    """Best-effort killpg of every currently-registered group. Does not raise: a group that's
    already gone (ProcessLookupError) or not ours to signal (PermissionError) is skipped, so one
    stale entry never stops the rest from being reaped. Registration is left untouched -- callers
    reaping on shutdown don't care, and callers reaping mid-run rely on the spawner's own
    `finally` to unregister once its Popen actually exits."""
    with _lock:
        pgids = list(_pgids)
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
