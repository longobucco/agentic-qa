"""G0.5 live half (needs a sandbox; zero agent/LLM cost -- same cost profile as
g0_brittleness.py): does our evaluate_official() agree with a REAL desktop_env.DesktopEnv on
the same live state?

No-op probe, like g0_brittleness.py: run the task's config, do NOT act, score with both. This
isolates dispatch-logic fidelity from agent behavior -- the only variable is which code computed
the reward, not what happened on the desktop.

DesktopEnv is bootstrapped without its own VM provider: __init__ normally powers on a VM via
provider_name (vmware/aws/docker/...), which we don't want -- we already have a live sandbox.
__new__ skips __init__ entirely; the controller/setup_controller it would have built are built by
hand instead, pointed at our sandbox (same http_server-override pattern as
env/osworld_eval.py::make_setup_controller). _set_task_info() -- the real upstream resolution of
evaluator/metric/getter from the task JSON -- doesn't touch the VM at all, so it runs unmodified.

Usage (provision a sandbox first, then pass its controller URL):
  python -m benchmarks.osworld.env.sandbox up
  python -m benchmarks.osworld.analysis.g0_replication_fidelity_live <controller_url> [per_class]
"""
import sys
import tempfile
import time
from urllib.parse import urlparse

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g0_evaluator_audit as g0
from benchmarks.osworld.analysis import g0_replication_fidelity as g0r
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.env import sandbox as sb
from benchmarks.osworld.env.controller import Controller

# same reliable-config preference as g0_brittleness.py -- a FAILURE/SUCCESS mismatch should
# reflect the two evaluate() implementations, not a flaky config step
RELIABLE_APPS = {"libreoffice_calc", "libreoffice_writer", "libreoffice_impress", "gimp", "vlc"}


def _dispatch_type(task):
    ev = task.get("evaluator", {}) or {}
    func = ev.get("func")
    if func == "infeasible":
        return "infeasible"
    if isinstance(func, list):
        return "multi_" + ev.get("conj", "and")
    return "single"


def sample(per_class=2):
    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    unscorable = {a["id"] for a in g0r.coverage()["affected_tasks"]}
    tasks = g0._load_tasks()
    inscope = [t for t in tasks
               if set(t.get("related_apps") or []) <= sup and t["id"] not in unscorable]

    out = {}
    for dt in ("single", "multi_and", "multi_or", "infeasible"):
        pool = [t for t in inscope if _dispatch_type(t) == dt]
        pool.sort(key=lambda t: (not set(t.get("related_apps") or []) <= RELIABLE_APPS, t["id"]))
        out[dt] = pool[:per_class]
    return out


def _bootstrap_real_desktop_env(controller_url, task, cache_dir_base):
    """A real DesktopEnv, minus its VM provider -- see module docstring."""
    from desktop_env.desktop_env import DesktopEnv
    from desktop_env.controllers.python import PythonController

    env = DesktopEnv.__new__(DesktopEnv)
    env.enable_proxy = False
    env.client_password = "password"
    env.screen_width, env.screen_height = 1920, 1080
    u = urlparse(controller_url)
    env.vm_ip = u.hostname or "localhost"
    env.server_port = u.port or (443 if u.scheme == "https" else 5000)
    env.chromium_port = 9222
    env.vlc_port = 8080
    env.controller = PythonController(vm_ip=env.vm_ip, server_port=env.server_port)
    env.controller.http_server = controller_url.rstrip("/")
    env.setup_controller = osworld_eval.make_setup_controller(controller_url)
    env.action_history = []
    env.cache_dir_base = cache_dir_base
    env._set_task_info(task)
    return env


_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 5


def _retry(fn, *, attempts=_RETRY_ATTEMPTS, delay=_RETRY_DELAY_S):
    """Same reasoning as agent_computer.py::_evaluate_with_retry: the Daytona proxy drops
    connections under load (ConnectionResetError / RemoteDisconnected), transiently, on both
    the setup and scoring calls this probe makes -- observed live losing an entire sample to it
    on the first two live runs. Retries on ANY exception (not just requests.ConnectionError):
    this probe calls three different layers (config setup, our evaluator, the real DesktopEnv),
    each wrapping network errors in its own exception type, not worth special-casing here."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last_err


def probe(controller_url, task, dispatch_type):
    ctrl = Controller(controller_url)
    # _run_config swallows its own exceptions into a string return (never raises), so _retry's
    # except-based loop wouldn't retry it -- retry on a truthy (error) return instead.
    setup_err = None
    for attempt in range(_RETRY_ATTEMPTS):
        setup_err = sb._run_config(ctrl, task)
        if not setup_err:
            break
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_S)
    if setup_err:
        return {"id": task["id"], "dispatch_type": dispatch_type, "outcome": "ENVIRONMENT_ERROR",
                "detail": setup_err}

    try:
        ours = _retry(lambda: osworld_eval.evaluate_official(controller_url, task, action_history=[]))
    except Exception as e:
        return {"id": task["id"], "dispatch_type": dispatch_type, "outcome": "OURS_RAISED",
                "detail": f"{type(e).__name__}: {e}"}

    def _real():
        env = _bootstrap_real_desktop_env(controller_url, task, tempfile.mkdtemp(prefix="g05_live_"))
        return env.evaluate()

    try:
        real = _retry(_real)
    except Exception as e:
        return {"id": task["id"], "dispatch_type": dispatch_type, "outcome": "REAL_RAISED",
                "ours": ours, "detail": f"{type(e).__name__}: {e}"}

    return {"id": task["id"], "dispatch_type": dispatch_type, "outcome": "compared",
            "ours": ours, "real": real, "agree": float(ours or 0) == float(real or 0)}


def run(controller_url, per_class=2):
    results = []
    for dt, tasks in sample(per_class).items():
        for t in tasks:
            r = probe(controller_url, t, dt)
            results.append(r)
            print(r)
    return results


def summarize(results):
    compared = [r for r in results if r["outcome"] == "compared"]
    agree = sum(1 for r in compared if r["agree"])
    print(f"\nagreement: {agree}/{len(compared)} compared (ours vs real DesktopEnv.evaluate())")
    disagree = [r for r in compared if not r["agree"]]
    for r in disagree:
        print(f"  DISAGREE {r['id']} ({r['dispatch_type']}): ours={r['ours']} real={r['real']}")
    other = [r for r in results if r["outcome"] != "compared"]
    if other:
        print(f"\nnot compared ({len(other)}):")
        for r in other:
            print(f"  {r['id']} ({r['dispatch_type']}) {r['outcome']}: {r.get('detail','')[:100]}")


def _main(argv):
    if not argv:
        print(__doc__)
        return
    per_class = int(argv[1]) if len(argv) > 1 else 2
    results = run(argv[0], per_class)
    summarize(results)


if __name__ == "__main__":
    _main(sys.argv[1:])
