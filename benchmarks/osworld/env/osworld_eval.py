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
    return {"evaluator_commit": commit if in_effect else None,
            "evaluator_package": installed,
            "evaluator_source": "pinned" if in_effect else "installed"}


def make_setup_controller(controller_url, *, cache_dir=None):
    """A real SetupController pointed at our controller_url (config/postconfig dispatch is real
    host-side logic per step type, not a 1:1 REST route name — don't hand-roll it)."""
    from desktop_env.controllers.setup import SetupController
    u = urlparse(controller_url)
    sc = SetupController(vm_ip=u.hostname or "localhost",
                         server_port=u.port or (443 if u.scheme == "https" else 5000),
                         cache_dir=cache_dir or tempfile.mkdtemp(prefix="osw_setup_cache_"))
    sc.http_server = controller_url.rstrip("/")
    sc.http_server_setup_root = controller_url.rstrip("/") + "/setup"
    return sc


def reward_to_verdict(reward):
    return "SUCCESS" if reward is not None and float(reward) >= 1.0 else "FAILURE"


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
                 getter_address=None, use_proxy=False):
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
        self.chromium_port = 9222
        self.vlc_port = 8080
        self._vm_machine = None
        # False for every existing (closed-book) caller. The open-book runner passes True here so
        # a getter that has to relaunch Chrome mid-evaluation (its CDP connection dropped) reads
        # this and relaunches WITH --proxy-server, same as chrome.py's own
        # get_gotoRecreationPage_and_get_html_content fallback already does when it's set.
        self.current_use_proxy = use_proxy
        self.action_history = action_history
        self.controller = controller

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


def evaluate_official(controller_url, task, action_history, cache_dir=None, use_proxy=False):
    """Return OSWorld's reward for this task (0..1), or None if desktop_env isn't importable.

    `cache_dir`: where the official getters land any gold reference they download. Pass one to
    hash those afterwards (hash_gold_artifacts); omitted, a throwaway temp dir is used, which
    keeps the historical behaviour for callers that don't care.

    `use_proxy`: False for every closed-book/baseline caller (unchanged behavior). The open-book
    runner passes True so a postconfig step that relaunches Chrome (several chrome-bucket tasks
    do: pkill then relaunch right before scoring) gets --proxy-server too, and so
    current_use_proxy reads true for getters that branch on it (see _EnvAdapter)."""
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
        address = split_for_getters(controller_url, fwd)
        controller = PythonController(vm_ip=address[0], server_port=address[1])
        env = _EnvAdapter(controller, controller_url, action_history, cache_dir=cache_dir,
                          getter_address=address, use_proxy=use_proxy)
        return _score(env, ev, func, controller_url, cache_dir, getters, metrics,
                      use_proxy=use_proxy)


def _score(env, ev, func, controller_url, cache_dir, getters, metrics, use_proxy=False):
    """The scoring pass itself, with the forwarder already up and `env` already addressed."""

    postconfig = ev.get("postconfig", [])
    if postconfig:
        # reuse the caller's cache_dir instead of minting a fresh one here -- the caller (see
        # agent_computer._score) already owns cleanup of the one it passed in; a second,
        # uncleaned mkdtemp per scored run is exactly the kind of per-run temp-dir leak that
        # let 5400 stray dirs/files (3.6GB) accumulate over ~1000 run attempts (found live
        # 2026-08-16).
        make_setup_controller(controller_url, cache_dir=cache_dir).setup(
            postconfig, use_proxy=use_proxy)

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
