"""Unit tests for g8_failure_taxonomy.py (offline, no sandbox, synthetic raw data):
  python -m benchmarks.osworld.tests.test_g8_failure_taxonomy
"""
import json
import tempfile
from pathlib import Path

from benchmarks.osworld.analysis import g8_failure_taxonomy as g8


def _write_run(base, task_id, run_idx, verdict, reason="", answer="DONE", **result):
    d = base / "agent_computer" / task_id / f"run_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "eval.json").write_text(json.dumps({"id": task_id, "verdict": verdict,
                                             "reason": reason}))
    (d / "result.json").write_text(json.dumps({"id": task_id, "answer": answer, **result}))
    return d


def _tmp():
    return Path(tempfile.mkdtemp(prefix="osw_g8_"))


def test_partial_tasks_are_excluded_from_the_completed_cohort():
    """The whole premise of the phase: a task caught mid-campaign has a run mix biased by
    run order (the campaign is serial), so it must not contribute verdicts."""
    d = _tmp()
    _write_run(d, "complete", 1, "SUCCESS")
    _write_run(d, "complete", 2, "SUCCESS")
    _write_run(d, "complete", 3, "FAILURE")
    _write_run(d, "half", 1, "SUCCESS")
    _write_run(d, "half", 2, "SUCCESS")
    r = g8.taxonomy(results_dir=d)
    assert r["cohort"] == {"completed": 1, "partial": 1, "never_run": 0}
    assert r["n_runs"] == 3                       # only the completed task's runs
    assert r["verdicts"]["SUCCESS"] == 2


def test_an_unscored_run_dir_keeps_the_task_out_even_at_three_scored_runs():
    """A 4th run that ran but was never scored means the task is still in flight; counting
    its first three would freeze a verdict mix the campaign is about to change."""
    d = _tmp()
    for i in (1, 2, 3):
        _write_run(d, "t", i, "SUCCESS")
    stray = d / "agent_computer" / "t" / "run_4"
    stray.mkdir(parents=True)
    (stray / "result.json").write_text(json.dumps({"id": "t", "answer": "DONE"}))
    r = g8.taxonomy(results_dir=d)
    assert r["cohort"]["completed"] == 0
    assert r["cohort"]["partial"] == 1


def test_legacy_archive_dirs_are_never_collected():
    """run_1_evalerror_legacy & friends start with 'run_' but are retired data (core.results
    .is_run_dir). Counting them would resurrect exactly the runs an earlier fix retired."""
    d = _tmp()
    for i in (1, 2, 3):
        _write_run(d, "t", i, "SUCCESS")
    legacy = d / "agent_computer" / "t" / "run_1_evalerror_legacy"
    legacy.mkdir(parents=True)
    (legacy / "eval.json").write_text(json.dumps({"id": "t", "verdict": "EVAL_ERROR"}))
    r = g8.taxonomy(results_dir=d)
    assert r["cohort"]["completed"] == 1
    assert r["n_runs"] == 3
    assert "EVAL_ERROR" not in r["verdicts"]


def test_layers_separate_agent_oracle_and_infra():
    d = _tmp()
    _write_run(d, "t", 1, "FAILURE")
    _write_run(d, "t", 2, "EVAL_ERROR", reason="official eval errored: TypeError: boom")
    _write_run(d, "t", 3, "ENVIRONMENT_ERROR", reason="config setup failed: step 0")
    r = g8.taxonomy(results_dir=d)
    assert r["layers"] == {"AGENT": 1, "ORACLE": 1, "INFRA": 1}
    assert r["pass_rate"] == 0.0


def test_reason_clustering_collapses_the_same_crash_across_tasks():
    """Twelve tasks crashing in one upstream getter must read as ONE bug, not twelve --
    that collapse is what turns a run log into a bug catalogue."""
    d = _tmp()
    for tid, char in (("a", 7), ("b", 130)):
        for i in (1, 2, 3):
            _write_run(d, tid, i, "EVAL_ERROR",
                       reason=f"no eval_state (official eval errored: JSONDecodeError: "
                              f"Expecting value: line 1 column 1 (char {char}))")
    clusters = g8.taxonomy(results_dir=d)["clusters"]["EVAL_ERROR"]
    assert len(clusters) == 1
    ids, _sig = clusters[0]
    assert ids == ["a", "b"]


def test_done_but_failed_counts_only_scored_agent_runs():
    """An EVAL_ERROR run also carries answer=DONE, but the oracle never judged it -- folding
    it in would inflate the headline 'agent thought it was finished' number with runs where
    nobody disagreed with the agent."""
    d = _tmp()
    _write_run(d, "t", 1, "FAILURE", answer="DONE")
    _write_run(d, "t", 2, "EVAL_ERROR", answer="DONE")
    _write_run(d, "t", 3, "SUCCESS", answer="DONE")
    r = g8.taxonomy(results_dir=d)["done_but_failed"]["?"]
    assert r == {"n": 1, "of": 2}


def test_partial_reward_picks_up_only_non_binary_scores():
    d = _tmp()
    _write_run(d, "t", 1, "FAILURE", reason="official compare_images -> 0.85")
    _write_run(d, "t", 2, "FAILURE", reason="official compare_images -> 0.00")
    _write_run(d, "t", 3, "SUCCESS", reason="official compare_images -> 1.00")
    got = g8.taxonomy(results_dir=d)["partial_reward"]
    assert len(got) == 1 and got[0]["reward"] == 0.85


def test_flaky_classification_ignores_tasks_with_unscorable_runs():
    """A task whose runs mix SUCCESS with EVAL_ERROR is not evidence of agent instability --
    one of those runs was never judged. Only all-agent-verdict tasks get classified."""
    d = _tmp()
    _write_run(d, "mixed", 1, "SUCCESS")
    _write_run(d, "mixed", 2, "FAILURE")
    _write_run(d, "mixed", 3, "SUCCESS")
    _write_run(d, "tainted", 1, "SUCCESS")
    _write_run(d, "tainted", 2, "FAILURE")
    _write_run(d, "tainted", 3, "EVAL_ERROR")
    st = g8.taxonomy(results_dir=d)["stability"]
    assert st["flaky"] == ["mixed"]
    assert st["always_pass"] == [] and st["always_fail"] == []


def test_turn_cap_hits_are_recorded_per_task():
    d = _tmp()
    _write_run(d, "t", 1, "FAILURE", agent_num_turns=g8.TURN_CAP)
    _write_run(d, "t", 2, "FAILURE", agent_num_turns=g8.TURN_CAP + 1)
    _write_run(d, "t", 3, "SUCCESS", agent_num_turns=30)
    assert g8.taxonomy(results_dir=d)["turn_cap_hits"] == {"t": ["FAILURE", "FAILURE"]}


def test_report_on_real_raw_is_well_formed():
    r = g8.taxonomy()
    assert r["cohort"]["completed"] > 0
    assert sum(r["layers"].values()) == r["n_runs"]
    assert 0.0 <= r["pass_rate"] <= 1.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
