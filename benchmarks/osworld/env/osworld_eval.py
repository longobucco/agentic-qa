"""Score a task with OSWorld's official evaluators (desktop_env.evaluators).

Replicates DesktopEnv.evaluate(): resolve getters/metrics from the real registries, run them
against the live desktop, combine with conj/or, handle `infeasible` via the agent's final action.

desktop_env is imported lazily: without it, evaluate_official returns None and the runner
falls back to the offline checker in evaluate.py.
"""
import hashlib
import os
import pathlib
import sys
import tempfile
from urllib.parse import urlparse

from benchmarks.osworld import config
from benchmarks.osworld.data.download_data import UPSTREAM_COMMIT
from benchmarks.osworld.env.cdp_forwarder import CdpForwarder, CdpForwarderError
from benchmarks.osworld.env.http_forwarder import (LoopbackForwarder,
                                                   split_for_getters)


# Getter types that fetch from a live third-party service (see g2_temporal_oracle.py for the
# audit). Single source of truth -- g2_temporal_oracle imports this instead of duplicating it.
EXTERNAL_LIVE_GETTERS = {"cloud_file", "googledrive_file", "info_from_website",
                         "pdf_from_url", "gotoRecreationPage_and_get_html_content"}


def _specs(ev, key):
    v = ev.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def gold_dest_filenames(task):
    """dest filenames (relative to cache_dir) an EXPECTED-side external-live getter writes --
    the actual gold, not the agent's own result artifact.

    get_vm_file (result side) and get_cloud_file (expected side) write into the same
    cache_dir, so hashing the whole dir would also hash the agent's own output, which is
    supposed to vary between runs. This narrows hashing to just the EXPECTED-side dest
    name(s). (Result-side external-live getters are a different risk -- see G2's "result
    risk" -- not oracle drift.)"""
    ev = task.get("evaluator", {}) or {}
    names = []
    for spec in _specs(ev, "expected"):
        if not spec or spec.get("type") not in EXTERNAL_LIVE_GETTERS:
            continue
        dest = spec.get("dest")
        if isinstance(dest, list):
            names.extend(dest)
        elif dest:
            names.append(dest)
    return names


def hash_gold_artifacts(cache_dir, task):
    """sha256 of only the genuine gold reference file(s) for `task` -- not everything in
    cache_dir (see gold_dest_filenames).

    For ~half the task set the ground truth isn't in the repo -- the official getters fetch
    it over the network at scoring time (see analysis/g2_temporal_oracle.py). A verdict that
    flips between runs could mean the agent behaved differently, or the gold changed
    underneath us; a constant hash across the campaign rules out the latter, a changed one
    marks that run ORACLE_DRIFT.

    Returns {} for a hermetic task even if the agent's own result file sits in the same
    cache_dir. Relies on _EnvAdapter handing every evaluation a fresh mkdtemp -- a shared
    cache dir would hide drift since get_cloud_file skips re-downloading a cached file."""
    out = {}
    if not cache_dir or not os.path.isdir(cache_dir):
        return out
    for name in gold_dest_filenames(task):
        path = os.path.join(cache_dir, name)
        try:
            with open(path, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue   # getter failed to fetch it -> nothing to hash, not an error here
    return out


PINNED_EVALUATORS = config.DATA_DIR / "evaluators"


def use_pinned_evaluators():
    """Overlay the evaluator tree fetched at UPSTREAM_COMMIT onto the installed desktop_env.

    The task set and the guest image are pinned to one upstream commit; the library that
    computes the verdict was not pinned at all, and six tasks in the verified release call
    metrics/getters the installed release doesn't have (data/download_evaluators.py). Inserting
    the pinned tree at the front of `desktop_env.evaluators.__path__` makes every
    `desktop_env.evaluators.<x>` import resolve there first, while the installed package keeps
    providing the controllers and the third-party dependencies the evaluators import.

    Returns the commit in use, or None when the pinned tree isn't on disk (in which case
    scoring still runs, on the installed release -- `evaluator_provenance()` says which, so a
    verdict is never silently attributed to the wrong evaluator).
    """
    stamp = PINNED_EVALUATORS / "PINNED_COMMIT"
    if not config.PINNED_EVALUATORS or not stamp.is_file():
        return None
    try:
        import desktop_env.evaluators as evaluators
    except Exception:
        return None
    path = str(PINNED_EVALUATORS)
    if path not in evaluators.__path__:
        # Front of the list: submodule lookup is first-match, so this shadows the installed
        # metrics/ and getters/ without touching the rest of the package.
        evaluators.__path__.insert(0, path)
    _drop_installed_submodules(path)
    return stamp.read_text().strip()


PINNED_CONTROLLERS = config.DATA_DIR / "controllers"


def use_pinned_setup_controller():
    """Overlay controllers/setup.py fetched at UPSTREAM_COMMIT onto the installed desktop_env, the
    same way use_pinned_evaluators does for the evaluators: config/postconfig steps then run on
    the SetupController the task data was written for (data/download_evaluators.py says why).

    Returns the commit in use, or None when the pinned file isn't on disk (setup then runs on the
    installed release; evaluator_provenance() records which)."""
    stamp = PINNED_CONTROLLERS / "PINNED_COMMIT"
    if not config.PINNED_EVALUATORS or not stamp.is_file():
        return None
    use_pinned_evaluators()   # the pinned setup.py imports desktop_env.evaluators.metrics.utils
    try:
        import desktop_env.controllers as controllers
    except Exception:
        return None
    path = str(PINNED_CONTROLLERS)
    if path not in controllers.__path__:
        controllers.__path__.insert(0, path)
    mod = sys.modules.get("desktop_env.controllers.setup")
    if mod is not None and not str(getattr(mod, "__file__", "")).startswith(path):
        # Already imported from the installed release (desktop_env/__init__ pulls it in):
        # evict it, and the parent's attribute, so the next import resolves to the pinned file.
        del sys.modules["desktop_env.controllers.setup"]
        if getattr(controllers, "setup", None) is mod:
            delattr(controllers, "setup")
    return stamp.read_text().strip()


def _drop_installed_submodules(pinned_path):
    """Evict `desktop_env.evaluators.*` modules already loaded from the installed release.

    `__path__` only steers imports that haven't happened yet, and one has: importing
    `desktop_env.controllers.setup` (every run does, for config setup) pulls in
    `desktop_env.evaluators.metrics.utils`, which drags the whole installed metrics package
    into sys.modules. Without this eviction the overlay is a silent no-op -- measured on the
    2026-09-08 campaign tail, where 171 runs recorded `evaluator_commit` while the installed
    1.0.2 had actually computed every one of their verdicts. A provenance field that lies is
    worse than no field at all.

    Modules already handed out stay alive for whoever holds a reference (setup.py keeps its
    `compare_urls`); only subsequent imports are redirected. These are pure-function modules,
    so two copies coexisting costs nothing but memory.
    """
    for name, mod in list(sys.modules.items()):
        if not name.startswith("desktop_env.evaluators."):
            continue
        origin = getattr(mod, "__file__", None)
        if not origin or str(origin).startswith(pinned_path):
            continue
        del sys.modules[name]
        # Dropping it from sys.modules is not enough: the parent package still holds it as an
        # attribute, and `from desktop_env.evaluators import metrics` is satisfied by that
        # attribute without ever re-running the import machinery.
        parent_name, _, leaf = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and getattr(parent, leaf, None) is mod:
            delattr(parent, leaf)


def evaluator_provenance():
    """{"evaluator_commit": ..., "evaluator_package": ...} -- who computed the verdict.

    Recorded per run: a pass rate is only comparable against another one scored by the same
    evaluator, and this project has already been bitten once by an unrecorded pin (config.MODEL).
    """
    commit = use_pinned_evaluators()
    try:
        from importlib.metadata import version
        installed = version("desktop_env")
    except Exception:
        installed = None
    # Report where the metrics ACTUALLY resolve from, not where we asked them to: the overlay
    # can be defeated by an earlier import (see _drop_installed_submodules).
    where = None
    try:
        from desktop_env.evaluators import metrics
        where = str(pathlib.Path(metrics.__file__).resolve().parent)
    except Exception:
        pass
    in_effect = bool(commit) and where is not None and where.startswith(str(PINNED_EVALUATORS))
    # Same discipline for the SetupController that ran config/postconfig: where it resolves from.
    setup_commit = use_pinned_setup_controller()
    setup_where = None
    try:
        from desktop_env.controllers import setup as setup_mod
        setup_where = str(pathlib.Path(setup_mod.__file__).resolve())
    except Exception:
        pass
    setup_in_effect = (bool(setup_commit) and setup_where is not None
                       and setup_where.startswith(str(PINNED_CONTROLLERS.resolve())))
    return {"evaluator_commit": commit if in_effect else None,
            "evaluator_package": installed,
            "evaluator_source": "pinned" if in_effect else "installed",
            "setup_controller_commit": setup_commit if setup_in_effect else None}


def pinned_code_problems():
    """Why scoring/setup would NOT run on the code of the task-data commit, or [] if it would.
    A campaign must refuse to start on a checkout missing data/evaluators or data/controllers
    (python -m benchmarks.osworld.data.download_evaluators): the fallback to the installed
    desktop_env 1.0.2 is silent and loses tasks (see data/download_evaluators.py)."""
    if not config.PINNED_EVALUATORS:
        return ["OSW_PINNED_EVALUATORS=0: scoring on the installed desktop_env"]
    prov = evaluator_provenance()
    problems = []
    for key in ("evaluator_commit", "setup_controller_commit"):
        if prov.get(key) != UPSTREAM_COMMIT:
            problems.append(f"{key}={prov.get(key)!r}, expected {UPSTREAM_COMMIT} -- run "
                            f"python -m benchmarks.osworld.data.download_evaluators")
    return problems


def pinned_code_preflight():
    problems = pinned_code_problems()
    if problems:
        raise SystemExit("pinned OSWorld code not in effect: " + "; ".join(problems))


def make_setup_controller(controller_url, *, cache_dir=None, chromium_port=None, vlc_port=None,
                          client_password=""):
    """A real SetupController pointed at our controller_url (config/postconfig dispatch is real
    host-side logic per step type, not a 1:1 REST route name — don't hand-roll it).

    `chromium_port`/`vlc_port`/`client_password`: the kvm backend's published host ports (the
    official VM's 9222/8080 land on random ones) and the guest's sudo password; None/"" keep
    SetupController's own defaults, as every Daytona caller always had."""
    use_pinned_setup_controller()
    from desktop_env.controllers.setup import SetupController
    u = urlparse(controller_url)
    ports = {k: v for k, v in (("chromium_port", chromium_port), ("vlc_port", vlc_port))
             if v is not None}
    sc = SetupController(vm_ip=u.hostname or "localhost",
                         server_port=u.port or (443 if u.scheme == "https" else 5000),
                         cache_dir=cache_dir or tempfile.mkdtemp(prefix="osw_setup_cache_"),
                         client_password=client_password, **ports,
                         screen_width=config.SCREEN_WIDTH, screen_height=config.SCREEN_HEIGHT)
    sc.http_server = controller_url.rstrip("/")
    sc.http_server_setup_root = controller_url.rstrip("/") + "/setup"
    return sc


# Float noise only: an exact match computed through floating point can land a hair under 1.0
# (compare_audios -> 0.9999999999923035 on 778efd0a). A genuinely partial score stays a failure.
_SUCCESS_TOLERANCE = 1e-6


def reward_to_verdict(reward):
    ok = reward is not None and float(reward) >= 1.0 - _SUCCESS_TOLERANCE
    return "SUCCESS" if ok else "FAILURE"


def _last_is_fail(action_history):
    if not action_history:
        return False
    last = action_history[-1]
    return last == "FAIL" or (isinstance(last, dict) and last.get("action_type") == "FAIL")


def _guest_machine(controller, default="x86_64"):
    try:
        out = controller.execute_python_command("import platform; print(platform.machine())")
        text = (out or {}).get("output") if isinstance(out, dict) else out
        return (text or "").strip() or default
    except Exception:
        return default


class _EnvAdapter:
    """Minimal DesktopEnv stand-in that OSWorld's getters read from."""

    def __init__(self, controller, controller_url, action_history, cache_dir=None,
                 getter_address=None, use_proxy=False, chromium_port=None, vlc_port=None,
                 client_password=""):
        # What the twelve URL-building getters will interpolate into "http://{ip}:{port}".
        # Splitting the https:// controller URL here is what made them talk plain HTTP to port
        # 443 (env/http_forwarder.py); the loopback forwarder is passed in instead.
        self.vm_ip, self.server_port = getter_address or split_for_getters(controller_url, None)
        # caller passes its own dir to hash the gold afterwards (see hash_gold_artifacts),
        # even if scoring raises -- the gold may already be on disk by then
        self.cache_dir = cache_dir or tempfile.mkdtemp(prefix="osw_eval_cache_")
        # getters branch on this against literal 'Windows'/'Darwin'/'Linux' (e.g.
        # chrome.py::get_default_search_engine) -- "Ubuntu" matches none and raises.
        # Found live on chrome-bucket tasks.
        self.vm_platform = "Linux"
        # Ports/flags the official getters read straight off the env. Values match
        # DesktopEnv's own defaults (desktop_env.py:148-153) -- our sandbox exposes the
        # guest's own ports, so the upstream defaults are the correct ones here, not the
        # per-provider overrides DesktopEnv computes for port-mapped Docker.
        # Omitting them raised AttributeError *inside* the getter, which the runner then
        # filed as EVAL_ERROR: 6 runs across 2 vlc tasks lost to a missing `vlc_port`
        # alone (inventory 2026-09-04). chromium_port is read in 9 places and
        # current_use_proxy in 2, so both were latent failures waiting on the right task.
        # `chromium_port`: 9222 unless a CdpForwarder is up (open-book only, use_proxy=True) --
        # see env/cdp_forwarder.py for why the literal guest port is otherwise unreachable from
        # the harness host (oracle_unroutable) and what makes it reachable after all.
        # On the kvm backend both are the container's published host ports (env/kvm_vm.py).
        self.chromium_port = chromium_port or 9222
        self.vlc_port = vlc_port or 8080
        self.client_password = client_password
        self._vm_machine = None
        # False for every existing (closed-book) caller. The open-book runner passes True here so
        # a getter that has to relaunch Chrome mid-evaluation (its CDP connection dropped) reads
        # this and relaunches WITH --proxy-server, same as chrome.py's own
        # get_gotoRecreationPage_and_get_html_content fallback already does when it's set.
        self.current_use_proxy = use_proxy
        self.action_history = action_history
        self.controller = controller
        self._controller_url = controller_url
        self._setup_controller = None

    @property
    def vm_machine(self):
        """Guest CPU architecture, asked for lazily.

        The pinned chrome getters branch on it (`env.vm_machine.lower()`), and its absence cost
        3 runs on 873cafdd the day the pinned tree went in. Lazy on purpose: an evaluator that
        short-circuits (the agent answered FAIL) must reach the guest zero times, and a probe in
        __init__ would spend a round-trip on every scored run to serve the handful of chrome
        getters that read it."""
        if self._vm_machine is None:
            self._vm_machine = _guest_machine(self.controller)
        return self._vm_machine

    @property
    def setup_controller(self):
        """A real SetupController on the guest, built lazily. The official vscode_config getter
        calls env.setup_controller._activate_window_setup(...) before replaying the command
        palette; without this attribute every such task ended as EVAL_ERROR (AttributeError) and
        was silently dropped from the scored population (53ad5833, 4 runs, found 2026-09-24)."""
        if self._setup_controller is None:
            self._setup_controller = make_setup_controller(
                self._controller_url, cache_dir=self.cache_dir, chromium_port=self.chromium_port,
                vlc_port=self.vlc_port, client_password=self.client_password)
        return self._setup_controller


def evaluate_official(controller_url, task, action_history, cache_dir=None, use_proxy=False,
                       enable_cdp_forwarder=True, chromium_port=None, vlc_port=None,
                       client_password=""):
    """Return OSWorld's reward for this task (0..1), or None if desktop_env isn't importable.

    `cache_dir`: where the official getters land any gold reference they download. Pass one to
    hash those afterwards (hash_gold_artifacts); omitted, a throwaway temp dir is used, which
    keeps the historical behaviour for callers that don't care.

    `use_proxy`: False for every closed-book/baseline caller (unchanged behavior). The open-book
    runner passes True so a postconfig step that relaunches Chrome (several chrome-bucket tasks
    do: pkill then relaunch right before scoring) gets --proxy-server too, and so
    current_use_proxy reads true for getters that branch on it (see _EnvAdapter).

    `enable_cdp_forwarder`: independent of `use_proxy` (2026-09-23 split, see env/sandbox.py's
    matching split on `_run_config` for the full rationale -- the two used to be one flag,
    conflating the open-book-only --proxy-server injection with a CDP-routing fix that both
    books need equally). Activates a CdpForwarder (env/cdp_forwarder.py), making Chrome's CDP
    port reachable for get_open_tabs_info/get_active_tab_info/get_active_tab_html_parse --
    otherwise blocked by the same defect this module already works around for the controller's
    own HTTP port (oracle_unroutable, see g9_replication_validity.py). Defaults True: every
    caller wants CDP routing to work, so the CdpForwarder.start() probe (which raises and is
    caught harmlessly when nothing is listening on Chrome's CDP port yet) is unconditional
    unless a caller explicitly opts out.

    `chromium_port`/`vlc_port`/`client_password`: set by the kvm backend only (published host
    ports of the official VM, plus its sudo password). The getters build
    "http://{env.vm_ip}:{env.chromium_port}" (and the same for VLC), so with mapped ports the
    getters address the VM host directly -- the controller URL is plain http there, and the
    loopback front door would put 127.0.0.1 in front of a CDP port that lives on KVM_ADDR."""
    use_pinned_evaluators()     # must precede the import below: it decides what gets imported
    try:
        from desktop_env.controllers.python import PythonController
        from desktop_env.evaluators import getters, metrics
    except Exception:
        return None

    ev = task.get("evaluator", {}) or {}
    func = ev.get("func")
    # One loopback front door for the whole scoring pass: the controller and the getters then
    # address the guest identically, and the getters that build "http://{ip}:{port}" inline
    # end up with a URL that is actually true (env/http_forwarder.py).
    with LoopbackForwarder(controller_url) as fwd:
        address = split_for_getters(controller_url, None if chromium_port else fwd)
        controller = PythonController(vm_ip=address[0], server_port=address[1])
        cdp_fwd = None
        if use_proxy or enable_cdp_forwarder:
            try:
                cdp_fwd = CdpForwarder(
                    controller_url, controller_port=config.CONTROLLER_PORT).start()
            except CdpForwarderError as e:
                print(f"[osworld] WARNING: CdpForwarder unavailable ({e}); any getter needing "
                     f"Chrome's CDP port directly will fail exactly as it did before this fix")
        try:
            env = _EnvAdapter(controller, controller_url, action_history, cache_dir=cache_dir,
                              getter_address=address, use_proxy=use_proxy,
                              chromium_port=cdp_fwd.port if cdp_fwd else chromium_port,
                              vlc_port=vlc_port, client_password=client_password)
            return _score(env, ev, func, controller_url, cache_dir, getters, metrics,
                         use_proxy=use_proxy, cdp_forwarder=cdp_fwd, chromium_port=chromium_port,
                         vlc_port=vlc_port, client_password=client_password)
        finally:
            if cdp_fwd is not None:
                cdp_fwd.stop()


def _score(env, ev, func, controller_url, cache_dir, getters, metrics, use_proxy=False,
          cdp_forwarder=None, chromium_port=None, vlc_port=None, client_password=""):
    """The scoring pass itself, with the forwarder already up and `env` already addressed."""

    postconfig = ev.get("postconfig", [])
    if postconfig:
        if cdp_forwarder is not None:
            from benchmarks.osworld.env.cdp_forwarder import inject_remote_allow_origins
            postconfig = inject_remote_allow_origins(postconfig)
        # reuse the caller's cache_dir instead of minting a fresh one here -- the caller (see
        # agent_computer._score) already owns cleanup of the one it passed in; a second,
        # uncleaned mkdtemp per scored run is exactly the kind of per-run temp-dir leak that
        # let 5400 stray dirs/files (3.6GB) accumulate over ~1000 run attempts (found live
        # 2026-08-16).
        postconfig_ctrl = make_setup_controller(controller_url, cache_dir=cache_dir,
                                                chromium_port=chromium_port, vlc_port=vlc_port,
                                                client_password=client_password)
        if cdp_forwarder is not None:
            postconfig_ctrl.vm_ip, postconfig_ctrl.chromium_port = \
                cdp_forwarder.host, cdp_forwarder.port
        postconfig_ctrl.setup(postconfig, use_proxy=use_proxy)

    if func == "infeasible":
        return 1.0 if _last_is_fail(env.action_history) else 0.0
    if _last_is_fail(env.action_history):
        return 0.0

    conj = ev.get("conj", "and")
    metric = getattr(metrics, func) if isinstance(func, str) else [getattr(metrics, f) for f in func]

    def getter(spec):
        return getattr(getters, f"get_{spec['type']}") if spec else None

    if isinstance(metric, list):
        result_specs = ev["result"]
        expected_specs = ev.get("expected")
        options = [o or {} for o in (ev.get("options") or [{}] * len(metric))]
        results = []
        for i, m in enumerate(metric):
            try:
                result_state = getter(result_specs[i])(env, result_specs[i])
            except FileNotFoundError:
                if conj == "and":
                    return 0.0
                continue
            if expected_specs and expected_specs[i]:
                expected_state = getter(expected_specs[i])(env, expected_specs[i])
                score = m(result_state, expected_state, **options[i])
            else:
                score = m(result_state, **options[i])
            if conj == "and" and float(score) == 0.0:
                return 0.0
            if conj == "or" and float(score) == 1.0:
                return 1.0
            results.append(float(score))
        if not results:
            return 0.0
        return sum(results) / len(results) if conj == "and" else max(results)

    result_spec = ev["result"]
    options = ev.get("options") or {}
    try:
        result_state = getter(result_spec)(env, result_spec)
    except FileNotFoundError:
        return 0.0
    expected_spec = ev.get("expected")
    if expected_spec:
        expected_state = getter(expected_spec)(env, expected_spec)
        return float(metric(result_state, expected_state, **options))
    return float(metric(result_state, **options))


# Overlay the pinned evaluators as early as possible: at import of this module, before anything
# here can pull `desktop_env.controllers.setup` in (which imports the installed metrics). Doing
# it only inside evaluate_official meant evicting and re-importing modules mid-run, which also
# threw away any patch a caller had applied to them.
try:
    use_pinned_evaluators()
except Exception:      # never let provenance plumbing stop a run from being scored
    pass
