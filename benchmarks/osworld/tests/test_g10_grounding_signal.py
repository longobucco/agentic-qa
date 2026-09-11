"""Tests for analysis/g10_grounding_signal.py -- the targeting metric and the arm's gate.

Builds a throwaway results tree on disk rather than reading the real campaign, so the metric's
definition is pinned by construction: a run whose clicks and verdicts are known exactly, and whose
expected re-click count can be counted by hand.

One test here is a coupling guard rather than a behaviour test: the analysis recovers
click_element's resolved coordinates and its no-effect warning by matching strings that
mcp/grounding_tools.py emits. Reword either side and the metric silently reads zero, so the two
are asserted against each other.

  python -m benchmarks.osworld.tests.test_g10_grounding_signal
"""
import json
import tempfile
from pathlib import Path

from benchmarks.osworld.analysis import g10_grounding_signal as g10
from benchmarks.osworld.mcp import grounding_tools as gt
from benchmarks.osworld.tests.test_grounding_tools import TREE, FakeCtrl


def _transcript(calls):
    """calls: [(tool_name, input_dict, result_text)] -> conversation.jsonl lines in the real
    assistant/user tool_use + tool_result shape."""
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        cid = f"toolu_{i}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": name, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "content": result}]}}))
    return "\n".join(lines) + "\n"


def _tree(root, system, tasks):
    """tasks: {task_id: [(verdict, calls), ...]} -> a results tree on disk."""
    for tid, runs in tasks.items():
        for n, (verdict, calls) in enumerate(runs, 1):
            d = root / system / tid / f"run_{n}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "eval.json").write_text(json.dumps({"id": tid, "verdict": verdict}))
            (d / "conversation.jsonl").write_text(_transcript(calls))
    return root


# A task needs at least two runs to be classified into a stability bucket (same rule as
# g8_failure_taxonomy: one run is not evidence of always-pass or always-fail). Every fixture below
# therefore pairs the run under test with QUIET, a click-free run that only makes the task
# classifiable and contributes nothing to the click counts.
_C = "mcp__osworld__click"
_SHOT = "mcp__osworld__screenshot"
_TYPE = "mcp__osworld__type"
QUIET = [(_SHOT, {}, "<png>")]


def test_reclick_counts_a_repeat_within_the_threshold_only():
    with tempfile.TemporaryDirectory() as td:
        root = _tree(Path(td), "sys", {"t1": [("FAILURE", [
            (_C, {"x": 100, "y": 100}, "clicked"),
            (_C, {"x": 104, "y": 100}, "clicked"),     # 4px  -> re-click
            (_C, {"x": 400, "y": 400}, "clicked"),     # far  -> not
            (_C, {"x": 402, "y": 402}, "clicked"),     # ~3px -> re-click
        ]), ("FAILURE", QUIET)]})
        rep = g10.targeting(root, "sys")
        b = rep["buckets"]["always-fail"]
        assert b["clicks"] == 4 and b["reclicks"] == 2
        assert abs(b["reclick_rate"] - 0.5) < 1e-9


def test_typing_between_two_clicks_proves_the_first_landed():
    with tempfile.TemporaryDirectory() as td:
        root = _tree(Path(td), "sys", {"t1": [("FAILURE", [
            (_C, {"x": 100, "y": 100}, "clicked"),
            (_TYPE, {"text": "hello"}, "typed"),
            (_C, {"x": 100, "y": 100}, "clicked"),     # same spot, but typing intervened
        ]), ("FAILURE", QUIET)]})
        assert g10.targeting(root, "sys")["buckets"]["always-fail"]["reclicks"] == 0


def test_clicks_without_coordinates_are_excluded_from_the_denominator():
    """Real transcripts contain `click {}` calls. Counting them as clicks would inflate the
    denominator with actions whose target is unknown, which is not what the rate claims to
    measure."""
    with tempfile.TemporaryDirectory() as td:
        root = _tree(Path(td), "sys", {"t1": [("FAILURE", [
            (_C, {}, "clicked"),
            (_C, {"x": 100, "y": 100}, "clicked"),
        ]), ("FAILURE", QUIET)]})
        assert g10.targeting(root, "sys")["buckets"]["always-fail"]["clicks"] == 1


def test_buckets_split_by_verdict_agreement_across_runs():
    with tempfile.TemporaryDirectory() as td:
        one = [(_C, {"x": 10, "y": 10}, "clicked")]
        root = _tree(Path(td), "sys", {
            "pass1": [("SUCCESS", one), ("SUCCESS", one)],
            "fail1": [("FAILURE", one), ("FAILURE", one)],
            "flaky1": [("SUCCESS", one), ("FAILURE", one)],
            "single": [("SUCCESS", one)],            # < 2 runs: not classifiable
        })
        b = g10.targeting(root, "sys")["buckets"]
        assert b["always-pass"]["tasks"] == 1
        assert b["always-fail"]["tasks"] == 1
        assert b["flaky"]["tasks"] == 1


def test_click_element_coordinates_are_recovered_from_its_result_text():
    """The grounding tree's clicks carry no x/y in the call -- without this the arm would appear
    to make no clicks at all and its re-click rate would read as a vacuous zero."""
    with tempfile.TemporaryDirectory() as td:
        res = "clicked push-button 'Bold' at (212,62) [rank 1/3, score 1.00, matched name]"
        root = _tree(Path(td), "sys", {"t1": [("FAILURE", [
            ("mcp__osworld__click_element", {"description": "Bold"}, res),
            ("mcp__osworld__click_element", {"description": "Bold"}, res),   # same point
        ]), ("FAILURE", QUIET)]})
        b = g10.targeting(root, "sys")["buckets"]["always-fail"]
        assert b["clicks"] == 2 and b["reclicks"] == 1


def test_grounding_tool_usage_is_counted_including_no_effect_and_unresolved():
    with tempfile.TemporaryDirectory() as td:
        ok = "clicked menu 'Format' at (430,20) [rank 1/1, score 1.00, matched name]"
        dead = ("clicked push-button 'Bold' at (212,62) [rank 1/2]\n"
                "WARNING: the accessibility tree is unchanged -- this click had no visible effect.")
        none = "no element resolved for 'Nonexistent' -- fall back to reading the screenshot"
        root = _tree(Path(td), "sys", {"t1": [("FAILURE", [
            ("mcp__osworld__click_element", {"description": "Format"}, ok),
            ("mcp__osworld__click_element", {"description": "Bold"}, dead),
            ("mcp__osworld__click_element", {"description": "Nonexistent"}, none),
            ("mcp__osworld__find_element", {"description": "Bold"}, "1. (212,62) ..."),
            ("mcp__osworld__list_elements", {}, "3 element(s) matched"),
        ]), ("FAILURE", QUIET)]})
        rep = g10.targeting(root, "sys")
        g = rep["grounding_tools"]
        assert g["click_element"] == 3 and g["no_effect"] == 1 and g["unresolved"] == 1
        assert g["find_element"] == 1 and g["list_elements"] == 1
        # the unresolved call clicked nothing, so it must not enter the click denominator
        assert rep["buckets"]["always-fail"]["clicks"] == 2


def test_the_analysis_strings_still_match_what_the_tools_actually_emit():
    """Coupling guard. These are the only two facts the analysis recovers from free text, and both
    come from mcp/grounding_tools.py -- so derive them from that module rather than trusting the
    literals to have been kept in sync by hand."""
    real_ok = gt.click_element(FakeCtrl([TREE, TREE.replace('name="Bold"', 'name="Bolded"')]),
                               "Bold")
    m = g10._CLICKED_AT_RE.search(real_ok)
    assert m and (int(m.group(1)), int(m.group(2))) == (212, 62), real_ok

    real_dead = gt.click_element(FakeCtrl([TREE]), "Bold")       # tree never changes
    assert g10._NO_EFFECT in real_dead, real_dead

    real_none = gt.click_element(FakeCtrl([TREE]), "Publish to Salesforce")
    assert g10._UNRESOLVED in real_none, real_none


def test_two_proportion_z_matches_a_hand_computed_value():
    # 110/1000 vs 80/1000: pooled p = 0.095, se = sqrt(.095*.905*.002) = 0.013116...
    z = g10.two_proportion_z(110, 1000, 80, 1000)
    assert abs(z - 2.287) < 0.01
    assert g10.two_proportion_z(1, 0, 1, 10) is None
    assert g10.two_proportion_z(0, 10, 0, 10) == 0.0


def test_gate_requires_both_a_targeting_drop_and_outcome_improvements():
    def rep(reclicks, clicks, successes):
        # mirrors what targeting() produces, including reclick_rate=None on an empty tree
        return {"system": "x", "buckets": {}, "grounding_tools": {},
                "totals": {"clicks": clicks, "reclicks": reclicks,
                           "reclick_rate": (reclicks / clicks) if clicks else None},
                "successes_by_task": successes}

    shared = {"a", "b", "c", "d"}
    base = rep(100, 1000, {"a": 0, "b": 0, "c": 0, "d": 3})

    # targeting improves 50% and three tasks gain a success -> both conditions met
    assert g10._print_gate(base, rep(50, 1000, {"a": 1, "b": 1, "c": 1, "d": 3}), shared)
    # targeting improves but no outcome moves -> not passed
    assert not g10._print_gate(base, rep(50, 1000, {"a": 0, "b": 0, "c": 0, "d": 3}), shared)
    # outcomes improve but targeting does not -> not passed
    assert not g10._print_gate(base, rep(99, 1000, {"a": 1, "b": 1, "c": 1, "d": 3}), shared)
    # a tree with no clicks is inconclusive, never a pass
    assert not g10._print_gate(base, rep(0, 0, {"a": 1, "b": 1, "c": 1}), shared)


def test_a_missing_tree_yields_empty_metrics_rather_than_an_error():
    with tempfile.TemporaryDirectory() as td:
        rep = g10.targeting(Path(td), "does_not_exist")
        assert rep["totals"]["clicks"] == 0
        assert rep["totals"]["reclick_rate"] is None


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
