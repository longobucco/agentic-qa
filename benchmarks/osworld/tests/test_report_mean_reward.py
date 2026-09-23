"""OSWorld's own aggregation averages the evaluator's reward (partial credit included: e.g.
compare_images returns an SSIM such as 0.91). The binary SUCCESS rate is kept alongside; the mean
reward is the number comparable with published OSWorld scores."""
import json

from benchmarks.osworld import config, report


def _write(base, tid, run, verdict, reward):
    d = base / tid / f"run_{run}"
    d.mkdir(parents=True)
    (d / "eval.json").write_text(json.dumps({"id": tid, "verdict": verdict, "reward": reward}))


def test_mean_reward_counts_partial_credit_and_skips_unscored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    base = tmp_path / "sys"
    _write(base, "a", 1, "SUCCESS", 1.0)
    _write(base, "b", 1, "FAILURE", 0.5)
    _write(base, "c", 1, "FAILURE", 0.0)
    _write(base, "d", 1, "ENVIRONMENT_ERROR", None)
    _write(base, "e", 1, "EVAL_ERROR", None)
    got = report.official_mean_reward("sys")
    assert got == {"scored_runs": 3, "mean_reward": 0.5, "binary_success": 1 / 3}


def test_unparseable_reward_falls_back_to_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    _write(tmp_path / "sys", "a", 1, "SUCCESS", "n/a")
    assert report.official_mean_reward("sys")["mean_reward"] == 1.0
