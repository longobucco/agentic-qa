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


def register(pgid):
    """Record `pgid` as a live agent process group."""
    with _lock:
        _pgids.add(pgid)


def unregister(pgid):
    """Stop tracking `pgid` (call once the spawner's own Popen has finished, on every path --
    normal exit, timeout, or exception). Safe to call even if `pgid` was never registered."""
    with _lock:
        _pgids.discard(pgid)


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
