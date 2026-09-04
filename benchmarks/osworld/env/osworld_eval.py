"""Score a task with OSWorld's official evaluators (desktop_env.evaluators).

Replicates DesktopEnv.evaluate(): resolve getters/metrics from the real registries, run them
against the live desktop, combine with conj/or, handle `infeasible` via the agent's final action.

desktop_env is imported lazily: without it, evaluate_official returns None and the runner
falls back to the offline checker in evaluate.py.
"""
import hashlib
import os
import tempfile
from urllib.parse import urlparse


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


class _EnvAdapter:
    """Minimal DesktopEnv stand-in that OSWorld's getters read from."""

    def __init__(self, controller, controller_url, action_history, cache_dir=None):
        u = urlparse(controller_url)
        self.vm_ip = u.hostname or "localhost"
        self.server_port = u.port or (443 if u.scheme == "https" else 5000)
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
        self.current_use_proxy = False
        self.action_history = action_history
        self.controller = controller


def evaluate_official(controller_url, task, action_history, cache_dir=None):
    """Return OSWorld's reward for this task (0..1), or None if desktop_env isn't importable.

    `cache_dir`: where the official getters land any gold reference they download. Pass one to
    hash those afterwards (hash_gold_artifacts); omitted, a throwaway temp dir is used, which
    keeps the historical behaviour for callers that don't care."""
    try:
        from desktop_env.controllers.python import PythonController
        from desktop_env.evaluators import getters, metrics
    except Exception:
        return None

    ev = task.get("evaluator", {}) or {}
    func = ev.get("func")
    u = urlparse(controller_url)
    controller = PythonController(vm_ip=u.hostname or "localhost",
                                  server_port=u.port or (443 if u.scheme == "https" else 5000))
    controller.http_server = controller_url.rstrip("/")   # honor the full (Daytona proxy) URL
    env = _EnvAdapter(controller, controller_url, action_history, cache_dir=cache_dir)

    postconfig = ev.get("postconfig", [])
    if postconfig:
        # reuse the caller's cache_dir instead of minting a fresh one here -- the caller (see
        # agent_computer._score) already owns cleanup of the one it passed in; a second,
        # uncleaned mkdtemp per scored run is exactly the kind of per-run temp-dir leak that
        # let 5400 stray dirs/files (3.6GB) accumulate over ~1000 run attempts (found live
        # 2026-08-16).
        make_setup_controller(controller_url, cache_dir=cache_dir).setup(postconfig)

    if func == "infeasible":
        return 1.0 if _last_is_fail(action_history) else 0.0
    if _last_is_fail(action_history):
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
