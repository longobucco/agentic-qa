"""Unit tests for g3_anomaly_scan.py (offline, synthetic raw data):
  python -m benchmarks.osworld.tests.test_g3_anomaly_scan
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchmarks.osworld.analysis import g3_anomaly_scan as scan_mod
from benchmarks.osworld.analysis.g3_sample import full


def _write_run(base, task_id, run_idx, *, verdict="FAILURE", reason="x", duration_ms=100000,
               cost=1.0, turns=50, clean=True, started=None, finished=None):
    d = base / "agent_computer" / task_id / f"run_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "eval.json").write_text(json.dumps({"id": task_id, "verdict": verdict, "reason": reason}))
    prov = {}
    if started and finished:
        prov = {"started_at": started.isoformat(), "finished_at": finished.isoformat()}
    (d / "result.json").write_text(json.dumps({
        "id": task_id, "agent_duration_ms": duration_ms, "agent_cost_usd": cost,
        "agent_num_turns": turns, "agent_clean_finish": clean, "provenance": prov,
    }))


def test_wall_clock_gap_flags_sleep_signature():
    """The exact pattern found live 2026-08-09: agent billed ~36min but wall-clock spans
    ~10.6h because the host slept mid-run. Must be detected without a human diffing timestamps."""
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    start = datetime(2026, 8, 8, 21, 30, tzinfo=timezone.utc)
    end = start + timedelta(seconds=38145)
    _write_run(d, tid, 1, duration_ms=2192762, started=start, finished=end)
    r = scan_mod.scan(results_dir=d)
    assert len(r["wall_clock_gap"]) == 1
    assert r["wall_clock_gap"][0]["run"] == f"{tid}/run_1"


def test_wall_clock_gap_not_flagged_when_durations_match():
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    start = datetime(2026, 8, 8, 21, 30, tzinfo=timezone.utc)
    end = start + timedelta(seconds=950)
    _write_run(d, tid, 1, duration_ms=947000, started=start, finished=end)
    r = scan_mod.scan(results_dir=d)
    assert r["wall_clock_gap"] == []


def test_turn_cap_hit():
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, tid, 1, turns=151)
    _write_run(d, tid, 2, turns=80)
    r = scan_mod.scan(results_dir=d)
    assert len(r["turn_cap_hit"]) == 1
    assert r["turn_cap_hit"][0]["run"] == f"{tid}/run_1"


def test_reason_cluster_ignores_expected_failure_repetition():
    """Every infeasible task legitimately shares the reason 'official infeasible -> 0.00' --
    that's expected, not a systemic bug, and must not be flagged."""
    ids = full()[:2]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    for tid in ids:
        _write_run(d, tid, 1, verdict="FAILURE", reason="official infeasible -> 0.00")
    r = scan_mod.scan(results_dir=d)
    assert r["reason_cluster"] == []


def test_reason_cluster_flags_shared_eval_error():
    """Two different tasks hitting the identical exception string IS a signal -- this is how
    the shared chrome_inject_js gap across 030eeff7/2ae9ba84 would surface automatically."""
    ids = full()[:2]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    same_reason = "AssertionError: Setup controller cannot find init function _x_setup"
    for tid in ids:
        _write_run(d, tid, 1, verdict="EVAL_ERROR", reason=same_reason)
    r = scan_mod.scan(results_dir=d)
    assert len(r["reason_cluster"]) == 1
    assert set(r["reason_cluster"][0]["tasks"]) == set(ids)


def test_reason_cluster_ignores_single_task_repeating_across_its_own_runs():
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    same_reason = "AssertionError: x"
    _write_run(d, tid, 1, verdict="EVAL_ERROR", reason=same_reason)
    _write_run(d, tid, 2, verdict="EVAL_ERROR", reason=same_reason)
    _write_run(d, tid, 3, verdict="EVAL_ERROR", reason=same_reason)
    r = scan_mod.scan(results_dir=d)
    assert r["reason_cluster"] == []


def test_dirty_finish():
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, tid, 1, clean=False)
    _write_run(d, tid, 2, clean=True)
    r = scan_mod.scan(results_dir=d)
    assert len(r["dirty_finish"]) == 1


def test_duration_outlier_needs_enough_data_and_flags_the_far_value():
    tid = full()[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    for i, dur in enumerate([100000, 105000, 95000, 110000, 5000000], start=1):
        _write_run(d, tid, i, duration_ms=dur)
    r = scan_mod.scan(results_dir=d)
    flagged = {f["run"] for f in r["duration_outlier"]}
    assert f"{tid}/run_5" in flagged
    assert f"{tid}/run_1" not in flagged


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
