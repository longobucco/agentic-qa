"""Per-task Daytona desktop for OSWorld.

Provision a sandbox from the OSWorld image, run the task's config, yield a Controller; fresh
sandbox per task. Set OSW_CONTROLLER_URL or OSW_SANDBOX_ID to skip provisioning. Disk caps at
10GB, so the image must be slim.

Two things handled here rather than by hand:
  - Daytona's own init owns PID 1, so the controller is never auto-started -- on creation or
    on resuming a stopped sandbox. Every path that hands back a sandbox must (re-)exec it.
  - Daytona can auto-stop an idle sandbox well before auto_stop_interval. A reused sandbox
    (OSW_SANDBOX_ID) must be checked and resumed before use.

CLI: python -m benchmarks.osworld.env.sandbox up|down <id>|list|resume <id>
"""
import os
import sys
import time
from contextlib import contextmanager

from benchmarks.osworld import config
from benchmarks.osworld.env.controller import Controller
from core.dotenv import load_dotenv
from core.environment import Env

START_CMD = "supervisord -c /etc/supervisord.conf"
READY_POLL_TRIES = 60      # * 2s = up to 120s per attempt
READY_ATTEMPTS = 2         # attempts at (lock cleanup + start + poll)


def _client():
    load_dotenv()
    from daytona_sdk import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _exec(sandbox, cmd, timeout=300):
    r = sandbox.process.exec(f"bash -lc {_q(cmd)}", timeout=timeout)
    return getattr(r, "exit_code", None), (getattr(r, "result", "") or "").strip()


def _ensure_running(sb):
    """Resume a sandbox Daytona auto-stopped. No-op if it's already up."""
    if str(sb.state).endswith("STOPPED"):
        sb.start()
        sb.wait_for_sandbox_start()


def _ensure_controller_up(sb):
    """(Re-)start the in-guest controller and wait for it. Idempotent: no-op if already
    answering. Otherwise clears a leftover X11 lock (safety net for older images -- the
    current image's Xvfb -nolock avoids creating one) and execs supervisord if not running.

    Liveness check is `kill -0` on supervisord's own pidfile, not `pgrep -f <start cmd>` --
    pgrep -f also matches the `bash -lc "..."` wrapper's own argv, a false-positive self-match
    that silently skipped starting supervisord every time (exit 0, nothing ever spawned)."""
    ctrl = Controller(sb.get_preview_link(config.CONTROLLER_PORT).url)
    if ctrl.ready():
        return ctrl
    alive_check = "kill -0 $(cat /var/run/supervisord.pid 2>/dev/null) 2>/dev/null"
    for _ in range(READY_ATTEMPTS):
        _exec(sb, "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99", timeout=20)
        _exec(sb, f"({alive_check}) || {START_CMD}", timeout=120)
        for _ in range(READY_POLL_TRIES):
            if ctrl.ready():
                return ctrl
            time.sleep(2)
    raise RuntimeError(f"OSWorld controller never became ready on sandbox {sb.id}")


def provision(image=None, *, disk=10, memory=8, cpu=4, auto_stop=20):
    from daytona_sdk import CreateSandboxFromImageParams, Resources
    d = _client()
    sb = d.create(CreateSandboxFromImageParams(
        image=image or config.IMAGE, public=True,
        resources=Resources(cpu=cpu, memory=memory, disk=min(disk, 10)),
        auto_stop_interval=auto_stop,
    ), timeout=2400)
    ctrl = _ensure_controller_up(sb)
    return sb, ctrl


def _launch_binary(step):
    cmd = step.get("parameters", {}).get("command", "")
    tokens = cmd if isinstance(cmd, list) else cmd.split()
    return os.path.basename(tokens[0]) if tokens else None


def _process_running(ctrl, name):
    return bool((ctrl.execute(f"pgrep -f {name}", shell=True) or "").strip())


def _verify_launches(ctrl, steps, *, wait=3, retries=4):
    """SetupController._launch_setup doesn't raise on failure (unlike _open_setup) -- a missing
    app binary silently "succeeds", so a bad launch would otherwise surface as an agent FAILURE
    that was never the agent's fault. Independently confirm every launched process started."""
    binaries = {b for s in steps if s.get("type") == "launch" for b in [_launch_binary(s)] if b}
    if not binaries:
        return None
    for _ in range(retries):
        time.sleep(wait)
        binaries = {b for b in binaries if not _process_running(ctrl, b)}
        if not binaries:
            return None
    return f"launched app(s) never started: {', '.join(sorted(binaries))}"


def _run_config(ctrl, task):
    """Run the task's config via OSWorld's own SetupController, then independently verify any
    "launch" step actually started (see _verify_launches). Returns None on success, an error
    string on failure -- never silently swallowed. No fallback to a naive per-step POST: config
    dispatch is real host-side logic per step type, not a 1:1 REST route, and a naive POST 404s
    on "download"/"open". An unprepared environment is worse than none -- the caller must see
    the error and skip driving the agent rather than run it blind."""
    steps = task.get("config", [])
    if not steps:
        return None
    try:
        from benchmarks.osworld.env.osworld_eval import make_setup_controller
    except ImportError as e:
        return f"desktop_env not importable, cannot run config setup: {e}"
    try:
        make_setup_controller(ctrl.base_url).setup(steps)
    except Exception as e:
        return f"config setup failed: {e}"
    return _verify_launches(ctrl, steps)


_warned_reuse = False


def _warn_reuse_once(how):
    # printed once per process, not once per task, so a --limit 6 run doesn't spam it 6 times.
    global _warned_reuse
    if not _warned_reuse:
        print(f"[osworld] WARNING: {how} — reusing one desktop across tasks with NO state "
              f"reset between them (unlike the default fresh-sandbox-per-task path). Fine for a "
              f"spike/debug session; do not treat results from this mode as a valid run — a "
              f"later task can inherit an earlier task's leftover state.")
        _warned_reuse = True


@contextmanager
def osworld_environment(task, *, port=None):
    # priority: OSW_CONTROLLER_URL (running desktop) > OSW_SANDBOX_ID (existing) > fresh per task
    if config.CONTROLLER_URL:
        _warn_reuse_once("OSW_CONTROLLER_URL set")
        ctrl = Controller(config.CONTROLLER_URL)
        err = _run_config(ctrl, task)
        yield Env(port=None, browser=ctrl, setup_error=err)
        return

    sb = None
    try:
        if config.SANDBOX_ID:
            _warn_reuse_once("OSW_SANDBOX_ID set")
            d = _client()
            sb = next((s for s in d.list() if s.id == config.SANDBOX_ID), None)
            if sb is None:
                raise SystemExit(f"OSW_SANDBOX_ID={config.SANDBOX_ID} not found")
            _ensure_running(sb)
            ctrl = _ensure_controller_up(sb)
        else:
            sb, ctrl = provision()
        err = _run_config(ctrl, task)
        yield Env(port=None, browser=ctrl, setup_error=err)
    finally:
        if sb is not None and not config.SANDBOX_ID:   # tear down fresh sandboxes only
            try:
                sb.delete()
            except Exception:
                pass


def _main(argv):
    if not argv:
        print(__doc__); return
    cmd = argv[0]
    if cmd == "up":
        sb, ctrl = provision(argv[1] if len(argv) > 1 else None)
        print(f"sandbox={sb.id}\nOSW_CONTROLLER_URL={ctrl.base_url}\nready={ctrl.ready()}")
    elif cmd == "resume":
        d = _client()
        sb = next((s for s in d.list() if s.id == argv[1]), None)
        if sb is None:
            raise SystemExit(f"sandbox {argv[1]} not found")
        _ensure_running(sb)
        ctrl = _ensure_controller_up(sb)
        print(f"sandbox={sb.id}\nOSW_CONTROLLER_URL={ctrl.base_url}\nready={ctrl.ready()}")
    elif cmd == "down":
        d = _client()
        s = next((s for s in d.list() if s.id == argv[1]), None)
        if s is None:
            raise SystemExit(f"sandbox {argv[1]} not found")
        s.delete()
        print("deleted", argv[1])
    elif cmd == "list":
        for s in _client().list():
            print(s.id, getattr(s, "state", None))
    else:
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
