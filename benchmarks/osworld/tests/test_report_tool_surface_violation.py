"""Task 7b: tool-surface violations are terminal FAILUREs (eval.json carries
`tool_surface_violation`), reported as their own breakdown line so they stay distinguishable
from ordinary agent failures rather than silently averaged into the pass rate."""
import json

from benchmarks.osworld import config, report


def _write_eval(base, tid, run, rec):
    d = base / tid / f"run_{run}"
    d.mkdir(parents=True)
    (d / "eval.json").write_text(json.dumps({"id": tid, **rec}))


def test_tool_surface_violation_count_only_counts_the_flagged_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    base = tmp_path / "sys"
    _write_eval(base, "a", 1, {"verdict": "FAILURE", "reward": 0.0, "source": "harness",
                               "tool_surface_violation": ["Read"]})
    _write_eval(base, "b", 1, {"verdict": "FAILURE", "reward": 0.0, "source": "harness",
                               "tool_surface_violation": ["web_search"]})
    _write_eval(base, "c", 1, {"verdict": "SUCCESS", "reward": 1.0, "source": "official"})
    _write_eval(base, "d", 1, {"verdict": "FAILURE", "reward": 0.0, "source": "official"})
    assert report.tool_surface_violation_count("sys") == 2


def test_tool_surface_violation_count_zero_when_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    base = tmp_path / "sys"
    _write_eval(base, "a", 1, {"verdict": "SUCCESS", "reward": 1.0, "source": "official"})
    assert report.tool_surface_violation_count("sys") == 0
