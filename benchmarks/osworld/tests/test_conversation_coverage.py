"""Unit tests for conversation_coverage.py (offline, no sandbox, synthetic raw data):
  python -m benchmarks.osworld.tests.test_conversation_coverage
"""
import json
import tempfile
from pathlib import Path

from benchmarks.osworld.analysis import conversation_coverage as cc


def _tmp():
    return Path(tempfile.mkdtemp(prefix="osw_convo_"))


def _write_run(base, task_id, run_idx, *, convo=True, api_error=None):
    d = base / "agent_computer" / task_id / f"run_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps({"agent_api_error_status": api_error}))
    if convo:
        (d / "conversation.jsonl").write_text('{"type":"queue-operation"}\n')
    return d


def test_a_real_run_is_counted_real_not_stub():
    d = _tmp()
    _write_run(d, "t1", 1, api_error=None)
    cov = cc.coverage(results_dir=d)
    assert cov["t1"]["real"] == [1]
    assert cov["t1"]["stub"] == []


def test_a_rate_limited_run_is_counted_stub_not_real():
    """agent_computer.py's rate-limit path still writes a 9-line conversation.jsonl stub (the
    CLI still returns a session id to copy from) -- it must not be counted as a usable
    transcript just because the file exists."""
    d = _tmp()
    _write_run(d, "t1", 1, api_error=429)
    cov = cc.coverage(results_dir=d)
    assert cov["t1"]["real"] == []
    assert cov["t1"]["stub"] == [1]


def test_a_run_without_a_conversation_file_is_invisible():
    """A scored run that predates conversation capture (or was never captured) leaves no
    transcript -- it must not appear at all, not appear as an empty entry."""
    d = _tmp()
    _write_run(d, "t1", 1, convo=False)
    cov = cc.coverage(results_dir=d)
    assert "t1" not in cov


def test_mixed_task_reports_both_real_and_stub_runs():
    d = _tmp()
    _write_run(d, "t1", 1, api_error=None)
    _write_run(d, "t1", 2, api_error=429)
    _write_run(d, "t1", 3, api_error=None)
    cov = cc.coverage(results_dir=d)
    assert cov["t1"]["real"] == [1, 3]
    assert cov["t1"]["stub"] == [2]


def test_report_on_real_raw_is_well_formed():
    """Smoke test against whatever's actually on disk -- catches a crash on the real,
    much-messier dataset that a synthetic fixture can't."""
    cov = cc.coverage()
    for tid, d in cov.items():
        assert isinstance(d["real"], list)
        assert isinstance(d["stub"], list)
        assert d["real"] or d["stub"]


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
