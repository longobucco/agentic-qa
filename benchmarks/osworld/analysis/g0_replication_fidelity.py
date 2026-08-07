"""G0.5: does our replica of OSWorld's scoring mechanism (env/osworld_eval.py) behave like the
real DesktopEnv.evaluate() (desktop_env.py)? Offline, no sandbox, no agent cost -- static half
of G0.5 (see gap-research-plan.md). The live half (compare rewards against a real DesktopEnv
instance on the same desktop state) needs a sandbox and isn't here.

Two checks:
  fingerprint  -- hash DesktopEnv.evaluate()'s own source against the value recorded here at
                  last audit. A mismatch means desktop_env changed since -- the KNOWN_DIVERGENCES
                  entries below were verified against a specific source shape, not derived
                  structurally, so they don't survive a silent upstream change unexamined.
  coverage     -- every getter/metric type actually used across the 369 tasks still resolves in
                  the installed desktop_env (getattr(getters, f"get_{type}") / getattr(metrics,
                  func)) -- catches a renamed/removed upstream symbol before it surfaces as a
                  HARNESS_ERROR mid-campaign.

KNOWN_DIVERGENCES are hand-verified, not auto-diffed (a real semantic diff of two dispatch
functions isn't reliable to automate) -- each entry pairs a check that the upstream source still
has the shape that causes it with a check that our own code still behaves as documented.

Run: python -m benchmarks.osworld.analysis.g0_replication_fidelity
"""
import inspect
import json
from hashlib import sha256
from unittest.mock import patch

from benchmarks.osworld import config
from benchmarks.osworld.env import osworld_eval

# Recorded 2026-08-06 against desktop_env==1.0.2, pinned via UPSTREAM_COMMIT
# (data/download_data.py). Re-verify KNOWN_DIVERGENCES by hand on any mismatch.
EVALUATE_SRC_SHA256 = "f6dce831423a0bc35b039c44bdea98c4c659aa742cc15f3c47402cb9b0284134"


def _load_tasks():
    tasks = []
    with open(config.TASKS_FILE) as f:
        for line in f:
            tasks.append(json.loads(line))
    return tasks


def fingerprint():
    from desktop_env.desktop_env import DesktopEnv
    src = inspect.getsource(DesktopEnv.evaluate)
    current = sha256(src.encode()).hexdigest()
    return {"current": current, "pinned": EVALUATE_SRC_SHA256, "match": current == EVALUATE_SRC_SHA256}


def _upstream_or_filenotfound_bug_present():
    """Textual check that DesktopEnv.evaluate()'s multi-metric FileNotFoundError handler still
    has the shape found on 2026-08-06: on conj='or' it falls through to use `result_state`
    without having assigned it. If this returns False, upstream likely fixed it -- re-verify
    _our_continue_on_or_filenotfound below still matches the (now different) real behavior."""
    from desktop_env.desktop_env import DesktopEnv
    src = inspect.getsource(DesktopEnv.evaluate)
    return (
        "if self.metric_conj == 'and':\n                        return 0\n\n"
        '                if "expected" in self.evaluator' in src
    )


def _upstream_expected_none_typeerror_bug_present():
    """Textual check that DesktopEnv.evaluate()'s multi-metric branch still checks 'expected'
    presence GLOBALLY (not per-index) before indexing expected_getter[idx] -- found 2026-08-06.
    _set_evaluator_info documents that a per-index None is the intended way to mark "this metric
    doesn't need expected" in a multi-metric evaluator, but builds expected_getter[idx]=None for
    those entries; evaluate() then still calls expected_getter[idx](self, ...) for EVERY index
    whenever the evaluator-level 'expected' key is a non-empty list, i.e. None(self, ...) ->
    TypeError on any index whose own entry is None. If this returns False, upstream likely
    switched to a per-index check -- re-verify _our_handles_mixed_expected_none below."""
    from desktop_env.desktop_env import DesktopEnv
    src = inspect.getsource(DesktopEnv.evaluate)
    return (
        'if "expected" in self.evaluator and self.expected_getter and self.evaluator["expected"]:\n'
        '                    expected_state = self.expected_getter[idx]' in src
    )


def _our_handles_mixed_expected_none():
    """Our multi-metric branch must not crash on a task whose 'expected' list mixes None (the
    documented "this metric needs no expected" marker) with a real spec for other indices -- it
    should call the metric with just result_state for the None entries, never invoke a None
    getter. Offline: stubs the getter/metric resolution instead of hitting a live desktop."""
    task = {
        "evaluator": {
            "func": ["exact_match", "exact_match"],
            "conj": "and",
            "result": [{"type": "stub_no_expected"}, {"type": "stub_with_expected"}],
            "expected": [None, {"type": "stub_gold"}],
        }
    }

    def fake_getter(env, spec):
        return "same"

    def fake_metric(result, expected=None, **kw):
        return 1.0 if (expected is None or result == expected) else 0.0

    with patch("desktop_env.evaluators.getters.get_stub_no_expected", fake_getter, create=True), \
         patch("desktop_env.evaluators.getters.get_stub_with_expected", fake_getter, create=True), \
         patch("desktop_env.evaluators.getters.get_stub_gold", fake_getter, create=True), \
         patch("desktop_env.evaluators.metrics.exact_match", fake_metric, create=True):
        reward = osworld_eval.evaluate_official("http://localhost:5000", task, action_history=[])
    return reward == 1.0   # both sub-metrics score 1.0 -- the None-expected index didn't crash


def _our_continue_on_or_filenotfound():
    """Our multi-metric branch must not crash when a getter raises FileNotFoundError under
    conj='or' -- it should skip that sub-metric and keep scoring the rest. Offline: stubs the
    getter/metric resolution instead of hitting a live desktop."""
    task = {
        "evaluator": {
            "func": ["exact_match", "exact_match"],
            "conj": "or",
            "result": [{"type": "stub_missing"}, {"type": "stub_present"}],
            "expected": [{"type": "stub_missing"}, {"type": "stub_present"}],
        }
    }

    def fake_getter(env, spec):
        if spec["type"] == "stub_missing":
            raise FileNotFoundError("no such file")
        return "x"

    def fake_metric(result, expected, **kw):
        return 1.0 if result == expected else 0.0

    with patch("desktop_env.evaluators.getters.get_stub_missing", fake_getter, create=True), \
         patch("desktop_env.evaluators.getters.get_stub_present", fake_getter, create=True), \
         patch("desktop_env.evaluators.metrics.exact_match", fake_metric, create=True):
        reward = osworld_eval.evaluate_official("http://localhost:5000", task, action_history=[])
    return reward == 1.0   # the surviving sub-metric (stub_present) scores 1.0 under conj='or'


KNOWN_DIVERGENCES = [
    {
        "id": "conj-or-filenotfound",
        "found": "2026-08-06",
        "description": (
            "Multi-metric conj='or': upstream doesn't assign result_state before using it "
            "when a getter raises FileNotFoundError (NameError risk); we `continue` to the "
            "next sub-metric instead. Deliberately not reproduced -- a crash burns a real run "
            "for no data."
        ),
        "upstream_still_has_bug": _upstream_or_filenotfound_bug_present,
        "our_behavior_holds": _our_continue_on_or_filenotfound,
        "scope": lambda: sum(
            1 for t in _load_tasks()
            if isinstance(t.get("evaluator", {}).get("func"), list)
            and t["evaluator"].get("conj") == "or"
        ),
        "scope_of": "369 tasks (conj='or' multi-metric)",
    },
    {
        "id": "expected-mixed-none-typeerror",
        "found": "2026-08-06",
        "description": (
            "Multi-metric evaluator whose 'expected' list mixes None (the documented "
            "per-index 'no expected needed' marker) with a real spec at another index: "
            "upstream checks 'expected' presence at the evaluator level, not per-index, so "
            "it calls expected_getter[idx] even where that entry is None -> "
            "TypeError: 'NoneType' object is not callable. We check expected_specs[i] "
            "per-index instead -- deliberately not reproduced, same reasoning as the 'or' "
            "divergence above: a crash burns a real run for no data."
        ),
        "upstream_still_has_bug": _upstream_expected_none_typeerror_bug_present,
        "our_behavior_holds": _our_handles_mixed_expected_none,
        "scope": lambda: sum(
            1 for t in _load_tasks()
            if isinstance((exp := t.get("evaluator", {}).get("expected")), list)
            and any(e is None for e in exp) and any(e is not None for e in exp)
        ),
        "scope_of": "369 tasks (multi-metric, 'expected' mixes None and a real spec)",
    },
]


def _used_getter_types():
    types = set()
    for t in _load_tasks():
        ev = t.get("evaluator", {}) or {}
        for key in ("result", "expected"):
            specs = ev.get(key)
            specs = specs if isinstance(specs, list) else [specs] if specs else []
            for s in specs:
                if isinstance(s, dict) and s.get("type"):
                    types.add(s["type"])
    return types


def _used_metric_funcs():
    funcs = set()
    for t in _load_tasks():
        func = t.get("evaluator", {}).get("func")
        if isinstance(func, list):
            funcs.update(f for f in func if isinstance(f, str))
        elif isinstance(func, str):
            funcs.add(func)
    return funcs - {"infeasible"}   # dispatched separately, not a metrics.* symbol


def _task_evaluator_symbols(task):
    ev = task.get("evaluator", {}) or {}
    func = ev.get("func")
    funcs = func if isinstance(func, list) else [func] if func else []
    types = set()
    for key in ("result", "expected"):
        specs = ev.get(key)
        specs = specs if isinstance(specs, list) else [specs] if specs else []
        for s in specs:
            if isinstance(s, dict) and s.get("type"):
                types.add(s["type"])
    return set(funcs) - {"infeasible", None}, types


def coverage():
    """Getter/metric types actually used across the 369 tasks that don't resolve in the
    installed desktop_env -- AttributeError, deterministic, not a transient flake, so retrying
    never helps (see _evaluate_with_retry: only requests.exceptions.ConnectionError retries).
    _score() already demotes this to EVAL_ERROR rather than crashing or guessing (see evaluate.py),
    but a task hitting this can NEVER produce an official verdict until desktop_env adds the
    symbol -- worth knowing which ones before spending a campaign on them."""
    from desktop_env.evaluators import getters, metrics
    missing_getters = sorted(t for t in _used_getter_types() if not hasattr(getters, f"get_{t}"))
    missing_metrics = sorted(f for f in _used_metric_funcs() if not hasattr(metrics, f))

    sup = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    affected = []
    for t in _load_tasks():
        funcs, types = _task_evaluator_symbols(t)
        hit = (funcs & set(missing_metrics)) | (types & set(missing_getters))
        if hit:
            affected.append({"id": t["id"], "in_scope": set(t.get("related_apps") or []) <= sup,
                             "symbols": sorted(hit)})
    return {"missing_getters": missing_getters, "missing_metrics": missing_metrics,
            "affected_tasks": affected}


def _main():
    fp = fingerprint()
    print(f"DesktopEnv.evaluate() fingerprint: {'MATCH' if fp['match'] else 'MISMATCH'}")
    if not fp["match"]:
        print(f"  pinned  {fp['pinned']}")
        print(f"  current {fp['current']}")
        print("  desktop_env's evaluate() changed since the last audit -- re-verify every "
              "entry in KNOWN_DIVERGENCES by hand before trusting the checks below.\n")

    print(f"\nKnown divergences ({len(KNOWN_DIVERGENCES)}):")
    for d in KNOWN_DIVERGENCES:
        upstream_ok = d["upstream_still_has_bug"]()
        ours_ok = d["our_behavior_holds"]()
        n = d["scope"]()
        print(f"  [{d['id']}] found {d['found']}, scope {n}/{d['scope_of']}")
        print(f"    upstream shape unchanged: {upstream_ok}   our behavior holds: {ours_ok}")
        if not upstream_ok:
            print("    ACTION: upstream's source no longer matches -- re-diff by hand.")
        if not ours_ok:
            print("    ACTION: our own guard regressed -- this is a real bug now.")

    cov = coverage()
    print(f"\nGetter/metric coverage against the installed desktop_env:")
    print(f"  missing getters: {cov['missing_getters'] or 'none'}")
    print(f"  missing metrics: {cov['missing_metrics'] or 'none'}")
    aff = cov["affected_tasks"]
    in_scope = [a for a in aff if a["in_scope"]]
    if aff:
        print(f"  {len(aff)} task(s) can never get an official verdict (permanent, not "
              f"transient) -- {len(in_scope)} of them in the current 260-task scope:")
        for a in aff:
            flag = "" if a["in_scope"] else "  (already out of scope)"
            print(f"    {a['id']}  {a['symbols']}{flag}")


if __name__ == "__main__":
    _main()
