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
import io
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from benchmarks.osworld import config
from benchmarks.osworld.env.controller import Controller
from core.dotenv import load_dotenv
from core.environment import Env

START_CMD = "supervisord -c /etc/supervisord.conf"
READY_POLL_TRIES = 60      # * 2s = up to 120s per attempt
READY_ATTEMPTS = 2         # attempts at (lock cleanup + start + poll)

# Observed live (2026-08-12): a freshly-created sandbox auto-stopped mid-provisioning (idle gap
# between create() returning and the first exec against it), then the deprecated daytona_sdk's
# process.exec() blocked forever against the stopped VM -- no effective timeout, no exception,
# just poll() never returning. That froze an unattended --runs campaign for 4+ hours on ONE task
# out of 435 with no recovery. Every step below (create, controller-ready poll, upstream
# SetupController.setup()) is individually bounded EXCEPT the raw SDK exec call, so this
# wall-clock watchdog is the backstop: past _PROVISION_TIMEOUT_S the whole provision+configure
# step is treated as a hang and raised as a plain RuntimeError, which core.run's work() already
# catches and files as INFRA_FLAKE -- resume then retries the task's run untouched (see finally
# below: the sandbox reference is exposed to the caller via `holder` as soon as it exists, so a
# timed-out attempt still gets torn down instead of leaking a paid sandbox).
_PROVISION_TIMEOUT_S = int(os.environ.get("OSW_PROVISION_TIMEOUT", "600"))


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


# A blank Xvfb framebuffer (nothing painted onto it yet) is visually near-monochrome: one flat
# background color, maybe a cursor. A real, rendered desktop -- even a completely idle one with
# no app open, just openbox's own background/decorations -- has far more color variety than
# that. Counting distinct colors rather than checking for literal black/a specific RGB value
# means this makes no assumption about openbox's theme, the guest's resolution, or which app (if
# any) is on screen -- it works identically whether the eventual foreground is a browser, an
# office app, or nothing at all.
#
# The threshold itself was wrong from this gate's introduction: confirmed live 2026-09-21 by
# extracting the actual screenshot bytes from a real "black screen" Astra failure (task
# bedcedc4) and counting its colors directly -- a solid black desktop with only the mouse
# cursor drawn on it already produces ~10 distinct colors (31991/40000 px pure (0,0,0), the
# other ~9 colors being single antialiased edge pixels from the cursor icon), comfortably
# clearing the old threshold of 8. That means every gate check on an actually-black desktop was
# reporting "ready" -- the two-consecutive-reads fix in the same commit as this comment made the
# check happen twice, but twice-wrong is still wrong when the single-shot version was never
# discriminating in the first place. A real rendered desktop (confirmed against a genuine
# successful run, task bb5e4c0d with Chrome open) shows 800-1000+ colors on the same 200x200
# thumbnail -- two orders of magnitude more. 50 sits with wide margin above the ~10-color
# cursor-only noise floor and far below any genuine rendered content observed so far.
_DESKTOP_READY_COLOR_THRESHOLD = 50
_DESKTOP_READY_TIMEOUT_S = 30
_DESKTOP_READY_POLL_S = 2
# Confirmed live 2026-09-21 (Astra canary, task 3ce045a0): a desktop can pass a single-shot
# readiness check -- render real content the very first time it's looked at -- and then go
# solid black again within under a minute, before the agent's own first screenshot. A 70s
# pure-idle control probe against the same task/image never reproduced it, so this is a startup
# race that only some sandbox boots hit, not a steady-state one a longer single check would
# catch. Requiring the SAME desktop to render on two reads spaced apart, instead of trusting the
# first one, is what actually distinguishes "settled" from "mid-race".
_DESKTOP_READY_STABLE_CHECKS = 2
_DESKTOP_READY_STABLE_INTERVAL_S = 3


def _desktop_rendered(ctrl):
    """True once the guest's screenshot shows real visual content, not a blank/uninitialized
    framebuffer. Never raises -- a transient screenshot failure here means "not ready yet", the
    same as a genuinely blank one, so the caller's retry loop is the only place that gives up."""
    try:
        raw = ctrl.screenshot()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((200, 200))  # downsampling preserves color diversity, not detail; keeps
                                    # this cheap to run on every retry
        colors = img.getcolors(maxcolors=100000)
        # getcolors() returns None if the image has MORE than maxcolors distinct colors -- given
        # a 200x200 thumbnail (40000px) and maxcolors=100000, None can only mean "so much color
        # variety it didn't even need to give up counting", i.e. unambiguously rendered.
        return colors is None or len(colors) > _DESKTOP_READY_COLOR_THRESHOLD
    except Exception:
        return False


def _wait_for_desktop_ready(ctrl, *, timeout=_DESKTOP_READY_TIMEOUT_S, poll=_DESKTOP_READY_POLL_S,
                             stable_checks=_DESKTOP_READY_STABLE_CHECKS,
                             stable_interval=_DESKTOP_READY_STABLE_INTERVAL_S):
    """Block (bounded) until the guest desktop is actually painting real content, not just until
    the HTTP server is reachable (see Controller.ready(), which only checks /screenshot doesn't
    raise -- a blank Xvfb buffer returns a perfectly valid 200 OK). Returns None once ready, or a
    diagnostic error string if it never became ready within the timeout budget -- the caller
    treats a non-None return the same as any other setup failure (ENVIRONMENT_ERROR), so this
    task's run is correctly excluded from scoring rather than silently handed to the agent
    against a desktop that was never going to render.

    Deliberately unconditional -- runs whether or not the task has any `config` "launch" steps,
    unlike _verify_launches (which only checks apps the task's own config explicitly launches
    and is a no-op for a task with config=[]). Confirmed live: task 937087b6 (config=[], no
    launch step at all) still hit a blank/unrendered desktop -- a check that only fires for
    "launch" steps cannot catch that class of failure.

    Requires `stable_checks` consecutive rendered reads, spaced `stable_interval` seconds apart,
    before declaring ready (see _DESKTOP_READY_STABLE_CHECKS for why a single instantaneous read
    isn't enough). Any non-rendered read resets the streak, so a flicker right after an apparent
    pass does not get grandfathered in -- the two checks have to be back to back."""
    deadline = time.monotonic() + timeout
    consecutive = 0
    while True:
        if _desktop_rendered(ctrl):
            consecutive += 1
            if consecutive >= stable_checks:
                return None
        else:
            consecutive = 0
        if time.monotonic() >= deadline:
            return (f"desktop never rendered stable content within {timeout}s (screenshot "
                     f"stayed near-blank/monochrome, or rendered then relapsed before settling) "
                     f"-- Xvfb/openbox/D-Bus startup race, not specific to any task or app")
        time.sleep(stable_interval if consecutive else poll)


def provision(image=None, *, disk=10, memory=8, cpu=4, auto_stop=20, on_created=None):
    """`on_created(sb)`, if given, fires right after create() returns and before the
    (potentially hanging) controller-ready wait -- lets a caller capture the sandbox for
    teardown even if the next step never comes back (see _PROVISION_TIMEOUT_S above)."""
    from daytona_sdk import CreateSandboxFromImageParams, Resources
    d = _client()
    sb = d.create(CreateSandboxFromImageParams(
        image=image or config.IMAGE, public=True,
        resources=Resources(cpu=cpu, memory=memory, disk=min(disk, 10)),
        auto_stop_interval=auto_stop,
    ), timeout=2400)
    if on_created:
        on_created(sb)
    ctrl = _ensure_controller_up(sb)
    return sb, ctrl


def _launch_binary(step):
    cmd = step.get("parameters", {}).get("command", "")
    tokens = cmd if isinstance(cmd, list) else cmd.split()
    # Skip any leading shell-style KEY=VALUE env-var assignments (e.g. "VLC_VERBOSE=-1 vlc
    # --no-audio ...", used by 18 tasks in the official task set) -- otherwise tokens[0] is the
    # env assignment, not the actual binary, and _window_mapped's wmctrl -l (which lists window
    # titles, not command lines) can never match it. Confirmed live: this silently turned 4 real
    # closed-book tasks (8ba5ae7a, 9195653c, a5bbbcd5, 215dfd39) into false ENVIRONMENT_ERRORs
    # the moment the window-mapped check shipped -- _process_running's pgrep -f had tolerated it
    # by accident (substring match against the whole command line), wmctrl -l does not.
    while tokens and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tokens[0]):
        tokens = tokens[1:]
    return os.path.basename(tokens[0]) if tokens else None


def _process_running(ctrl, name):
    return bool((ctrl.execute(f"pgrep -f {name}", shell=True) or "").strip())


# Launch targets that never open a window (headless/background helpers) -- checking them for a
# mapped window would always fail after _verify_launches's retries, turning a perfectly working
# environment into a false failure. socat is the one confirmed real case: used as a CDP-
# forwarding TCP relay alongside google-chrome in 79 task configs (see Dockerfile.osworld's own
# comment on the socat package), and never draws a window. Exempt, not removed from checking
# entirely -- _process_running still catches a socat that dies on startup; only the window-map
# requirement is skipped for it.
_HEADLESS_LAUNCH_BINARIES = {"socat"}


def _window_mapped(ctrl, name):
    """wmctrl lists mapped (visible, painted) windows -- stricter than _process_running's pgrep,
    which only proves the process forked, not that it rendered. Falls back to True (don't block)
    if wmctrl itself errors, so a wmctrl hiccup never becomes a new false-failure mode on top of
    the one this is fixing. Also returns True unconditionally for known headless launch targets
    (see _HEADLESS_LAUNCH_BINARIES) -- they legitimately never map a window, so requiring one
    would itself be a false-failure mode.

    -x adds each window's WM_CLASS to wmctrl's output (WINDOW_ID DESKTOP_ID WM_CLASS.INSTANCE
    HOSTNAME TITLE) -- a stable, machine-readable app identifier that (unlike the free-form,
    human-facing window TITLE) usually echoes the binary's own name. Confirmed live: the launch
    binary "google-chrome" never appears in Chrome's window TITLE ("Google Chrome", a space, not
    a hyphen -- .lower() fixes case but not that), so a title-only check always failed for it,
    turning the single largest task category in the whole benchmark (~88 chrome tasks) into a
    false ENVIRONMENT_ERROR. Adding -x is a strict superset of the previous check (the title is
    still in the output, plus the class now too), so it cannot break any match that used to
    succeed. The hyphen/underscore-to-space normalization below is an extra generic safety net
    for any binary/window-identifier pair that still doesn't share exact punctuation even via
    WM_CLASS."""
    if name in _HEADLESS_LAUNCH_BINARIES:
        return True
    out = ctrl.execute("wmctrl -lx", shell=True)
    if out is None:
        return True
    if name.lower() in out.lower():
        return True
    normalize = lambda s: s.lower().replace('-', ' ').replace('_', ' ')
    return normalize(name) in normalize(out)


def _verify_launches(ctrl, steps, *, wait=3, retries=4):
    """SetupController._launch_setup doesn't raise on failure (unlike _open_setup) -- a missing
    app binary silently "succeeds", so a bad launch would otherwise surface as an agent FAILURE
    that was never the agent's fault. Independently confirm every launched process started AND
    mapped a window -- a cold-started process can be alive for several seconds before painting
    anything, during which the agent's first screenshot would be handed over blank (observed:
    closed-book tasks 215dfd39/a5bbbcd5/28cc3b7e run 1, filed as "blank screen for the entire
    session" -- a process-only check would have missed this)."""
    binaries = {b for s in steps if s.get("type") == "launch" for b in [_launch_binary(s)] if b}
    if not binaries:
        return None
    for _ in range(retries):
        time.sleep(wait)
        binaries = {b for b in binaries
                    if not (_process_running(ctrl, b) and _window_mapped(ctrl, b))}
        if not binaries:
            return None
    return f"launched app(s) never started/rendered: {', '.join(sorted(binaries))}"


def _run_config(ctrl, task, *, use_proxy=False, sandbox=None):
    """Run the task's config via OSWorld's own SetupController, then independently verify any
    "launch" step actually started (see _verify_launches). Returns None on success, an error
    string on failure -- never silently swallowed. No fallback to a naive per-step POST: config
    dispatch is real host-side logic per step type, not a 1:1 REST route, and a naive POST 404s
    on "download"/"open". An unprepared environment is worse than none -- the caller must see
    the error and skip driving the agent rather than run it blind.

    `use_proxy`: threaded into upstream SetupController.setup(steps, use_proxy=...) -- when True,
    any "launch" step starting google-chrome gets --proxy-server=http://127.0.0.1:18888 appended
    (setup.py:309-310). False (default) for every existing caller; only the open-book environment
    (env.guest_proxy having already made something real listen there) passes True. True ALSO
    activates a CdpForwarder (env/cdp_forwarder.py) and injects --remote-allow-origins=* into any
    Chrome launch step, making chrome_open_tabs/chrome_close_tabs steps (raw CDP from the harness
    host, previously always unroutable -- see g9_replication_validity.py's oracle_unroutable)
    actually work. `sandbox`: the Daytona sandbox object, passed straight to CdpForwarder for its
    authoritative get_preview_link() call -- omit only for callers that never provision one (e.g.
    OSW_CONTROLLER_URL/OSW_SANDBOX_ID reuse paths), where CdpForwarder falls back to a verified
    URL-pattern instead.

    Also gates on _wait_for_desktop_ready before anything else, unconditionally (even for a task
    with no config steps at all) -- see that function's own docstring for why Controller.ready()
    alone isn't a sufficient readiness signal.
    """
    not_ready = _wait_for_desktop_ready(ctrl)
    if not_ready:
        return not_ready
    steps = task.get("config", [])
    if not steps:
        return None
    try:
        from benchmarks.osworld.env.osworld_eval import make_setup_controller
    except ImportError as e:
        return f"desktop_env not importable, cannot run config setup: {e}"
    # own the cache dir so it can be cleaned up afterwards -- left to its own mkdtemp default,
    # every provisioned run leaks one uncleaned dir (found live 2026-08-16, part of a 5400-file/
    # 3.6GB leak across the temp-dir patterns in this codebase; see the matching fix in
    # agent_computer._score / osworld_eval.evaluate_official).
    cache_dir = tempfile.mkdtemp(prefix="osw_setup_cache_")
    cdp_fwd = None
    try:
        setup_ctrl = make_setup_controller(ctrl.base_url, cache_dir=cache_dir)
        if use_proxy:
            from benchmarks.osworld.env.cdp_forwarder import (
                CdpForwarder, CdpForwarderError, inject_remote_allow_origins)
            steps = inject_remote_allow_origins(steps)
            try:
                cdp_fwd = CdpForwarder(
                    ctrl.base_url, sandbox=sandbox, controller_port=config.CONTROLLER_PORT
                ).start()
                setup_ctrl.vm_ip, setup_ctrl.chromium_port = cdp_fwd.host, cdp_fwd.port
            except CdpForwarderError as e:
                print(f"[osworld] WARNING: CdpForwarder unavailable ({e}); any "
                     f"chrome_open_tabs/chrome_close_tabs step in this task's config will fail "
                     f"exactly as it did before this fix")
        setup_ctrl.setup(steps, use_proxy=use_proxy)
    except Exception as e:
        return f"config setup failed: {e}"
    finally:
        if cdp_fwd is not None:
            cdp_fwd.stop()
        shutil.rmtree(cache_dir, ignore_errors=True)
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


def _provision_and_configure(task, holder):
    """Runs in a worker thread so osworld_environment can bound it with a wall-clock timeout
    (see _PROVISION_TIMEOUT_S) -- the deprecated daytona_sdk gives no such guarantee itself.
    `holder["sb"]` is set as soon as a sandbox exists, so a timed-out caller can still tear it
    down instead of leaking a paid sandbox the worker thread is stuck holding."""
    if config.SANDBOX_ID:
        _warn_reuse_once("OSW_SANDBOX_ID set")
        d = _client()
        sb = next((s for s in d.list() if s.id == config.SANDBOX_ID), None)
        if sb is None:
            raise SystemExit(f"OSW_SANDBOX_ID={config.SANDBOX_ID} not found")
        holder["sb"] = sb
        _ensure_running(sb)
        ctrl = _ensure_controller_up(sb)
    else:
        sb, ctrl = provision(on_created=lambda s: holder.update(sb=s))
    return ctrl, _run_config(ctrl, task)


@contextmanager
def osworld_environment(task, *, port=None):
    # priority: OSW_CONTROLLER_URL (running desktop) > OSW_SANDBOX_ID (existing) > fresh per task
    if config.CONTROLLER_URL:
        _warn_reuse_once("OSW_CONTROLLER_URL set")
        ctrl = Controller(config.CONTROLLER_URL)
        err = _run_config(ctrl, task)
        yield Env(port=None, browser=ctrl, setup_error=err)
        return

    holder = {}
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_provision_and_configure, task, holder)
        try:
            ctrl, err = fut.result(timeout=_PROVISION_TIMEOUT_S)
        except FutureTimeoutError:
            raise RuntimeError(
                f"sandbox provisioning/setup exceeded {_PROVISION_TIMEOUT_S}s -- treated as a "
                f"hang, not a legitimate wait (observed live: sandbox auto-stopped mid-"
                f"provisioning, then the SDK's exec blocked against the stopped VM forever)"
            ) from None
        yield Env(port=None, browser=ctrl, setup_error=err)
    finally:
        ex.shutdown(wait=False)   # don't block the whole campaign on a still-stuck worker thread
        sb = holder.get("sb")
        if sb is not None and not config.SANDBOX_ID:   # tear down fresh sandboxes only
            try:
                sb.delete()
            except Exception:
                pass


def _provision_and_configure_openbook(task, holder):
    """Same shape as _provision_and_configure, plus: start the guest fixture proxy and lock down
    egress (env.guest_proxy) BEFORE running the task's config, so any "launch" step's
    --proxy-server=... (use_proxy=True) actually points at something already listening.

    Task-scoped preflight (open_book_preflight.task_check) runs FIRST, before provisioning --
    a task with no fixture bundle costs nothing beyond the check itself, rather than paying for
    a sandbox it was never going to be able to run against."""
    from benchmarks.osworld import open_book_preflight
    from benchmarks.osworld.env import guest_proxy
    if not config.OPENBOOK_IMAGE:
        return None, ("OSW_OPENBOOK_IMAGE not set -- the closed-book image (config.IMAGE) has "
                      "no mitmproxy/CA/iptables layer; refusing to provision against it")
    check = open_book_preflight.task_check(task)
    if not check["ready"]:
        return None, check["reason"]
    bundle_dir = Path(check["bundle_dir"])
    if config.SANDBOX_ID:
        _warn_reuse_once("OSW_SANDBOX_ID set")
        d = _client()
        sb = next((s for s in d.list() if s.id == config.SANDBOX_ID), None)
        if sb is None:
            raise SystemExit(f"OSW_SANDBOX_ID={config.SANDBOX_ID} not found")
        holder["sb"] = sb
        _ensure_running(sb)
        ctrl = _ensure_controller_up(sb)
    else:
        sb, ctrl = provision(image=config.OPENBOOK_IMAGE, on_created=lambda s: holder.update(sb=s))
    try:
        guest_proxy.start(ctrl, bundle_dir)
    except guest_proxy.GuestProxyError as e:
        return ctrl, f"open-book guest proxy setup failed: {e}"

    proxy_tags = open_book_preflight.proxy_tags_for(task["id"])
    _HOST_SIDE_CONFIG_TAGS = {"host_side_config_download", "external_oauth_service"}
    if not (proxy_tags & _HOST_SIDE_CONFIG_TAGS):
        return ctrl, _run_config(ctrl, task, use_proxy=True, sandbox=sb)

    # A "download" config step (SetupController._download_setup) runs requests.get() on the
    # HARNESS HOST, not in the guest -- confirmed live 2026-09-12 (see
    # astra_openbook_campaign_lock.json's known_issues.non_chrome_egress_not_proxied, which
    # this closes for the config-download case). A "googledrive" config step
    # (SetupController._googledrive_setup) is the same story with real pydrive2 calls instead --
    # route both through the exact same host_proxy machinery already built for get_cloud_file,
    # scoped to just this setup() call, attaching drive_mock only when the oauth tag is present.
    #
    # _download_setup ALSO uploads the fetched file back to the real Daytona controller
    # (POST .../setup/upload) in the same requests session -- confirmed live that a blanket
    # HTTP_PROXY intercepts that upload too (502 from our OWN fixture proxy, which has no entry
    # for the controller's URL). The controller's own host must always be excluded; 127.0.0.1/
    # localhost too, on the same reasoning gpt_astra_openbook._score_openbook documents (a
    # different SetupController call path may address the guest via a loopback forwarder rather
    # than the controller's real hostname -- not observed here, but cheap to guard against).
    from urllib.parse import urlparse
    from benchmarks.osworld.env import host_proxy
    controller_host = urlparse(ctrl.base_url).hostname
    no_proxy_hosts = ("127.0.0.1", "localhost")
    if controller_host:
        no_proxy_hosts += (controller_host,)
    try:
        with host_proxy.host_proxy(
            bundle_dir, needs_drive_mock="external_oauth_service" in proxy_tags,
        ) as handle, host_proxy.scoped_env(handle, no_proxy_hosts=no_proxy_hosts):
            err = _run_config(ctrl, task, use_proxy=True, sandbox=sb)
    except host_proxy.HostProxyError as e:
        return ctrl, f"open-book host proxy setup failed: {e}"
    return ctrl, err


@contextmanager
def osworld_openbook_environment(task, *, port=None):
    """Open-book counterpart of osworld_environment: identical provisioning, but the guest
    fixture proxy is up and the egress lockdown confirmed before the task's config (and
    therefore Chrome) ever runs. Does not support OSW_CONTROLLER_URL reuse -- a reused desktop's
    proxy/lockdown state from a PREVIOUS task is exactly the cross-task contamination the frozen
    manifest's per-task bundle is supposed to rule out; open-book campaigns always provision
    fresh (OSW_SANDBOX_ID reuse is still allowed, same as the baseline path, since guest_proxy.
    start() is re-applied idempotently every call regardless)."""
    holder = {}
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_provision_and_configure_openbook, task, holder)
        try:
            ctrl, err = fut.result(timeout=_PROVISION_TIMEOUT_S)
        except FutureTimeoutError:
            raise RuntimeError(
                f"open-book sandbox provisioning/setup exceeded {_PROVISION_TIMEOUT_S}s -- "
                f"treated as a hang, not a legitimate wait"
            ) from None
        yield Env(port=None, browser=ctrl, setup_error=err)
    finally:
        ex.shutdown(wait=False)
        sb = holder.get("sb")
        if sb is not None and not config.SANDBOX_ID:
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
