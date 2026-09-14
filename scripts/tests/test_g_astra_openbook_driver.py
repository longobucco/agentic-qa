"""Offline unit tests for scripts/g_astra_openbook_driver.py's batching/backoff logic (no
sandbox, no core.run subprocess -- patches subprocess.run and writes fake infra_error.json
files directly):
  python -m scripts.tests.test_g_astra_openbook_driver
"""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from scripts import g_astra_openbook_driver as driver


def _write_infra(run_dir, outcome):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "infra_error.json").write_text(json.dumps([{"outcome": outcome}]))


def test_batches_split_the_population_into_fixed_size_chunks():
    ids = [f"t{i}" for i in range(10)]
    batches = list(driver._batches(ids, 4))
    assert batches == [["t0", "t1", "t2", "t3"], ["t4", "t5", "t6", "t7"], ["t8", "t9"]]


def test_rate_limited_fraction_ignores_stale_pre_existing_infra_files():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(driver, "_RESULTS_DIR", Path(tmp)):
            run_dir = Path(tmp) / "t1" / "run_1"
            _write_infra(run_dir, "RATE_LIMITED")
            before = driver._snapshot_infra_mtimes(["t1"])
            # No new write after the snapshot -- this outcome is stale, not from "this batch".
            fraction = driver._batch_rate_limited_fraction(["t1"], before)
            assert fraction == 0.0


def test_rate_limited_fraction_counts_a_freshly_written_infra_file():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(driver, "_RESULTS_DIR", Path(tmp)):
            run_dir = Path(tmp) / "t1" / "run_1"
            before = driver._snapshot_infra_mtimes(["t1"])  # nothing exists yet
            time.sleep(0.01)
            _write_infra(run_dir, "RATE_LIMITED")
            fraction = driver._batch_rate_limited_fraction(["t1"], before)
            assert fraction == 1.0


def test_rate_limited_fraction_is_zero_when_the_fresh_outcome_is_a_different_infra_class():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(driver, "_RESULTS_DIR", Path(tmp)):
            run_dir = Path(tmp) / "t1" / "run_1"
            before = driver._snapshot_infra_mtimes(["t1"])
            time.sleep(0.01)
            _write_infra(run_dir, "HARNESS_ERROR")
            fraction = driver._batch_rate_limited_fraction(["t1"], before)
            assert fraction == 0.0


def test_main_backs_off_after_a_batch_that_is_mostly_rate_limited(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        results_dir = Path(tmp) / "results"
        summary_path = Path(tmp) / "summary.json"
        sleeps = []

        def fake_run(cmd, cwd=None):
            # Simulate core.run: every id in this invocation's --ids gets a fresh RATE_LIMITED.
            ids_idx = cmd.index("--ids") + 1
            for task_id in cmd[ids_idx:]:
                _write_infra(results_dir / task_id / "run_1", "RATE_LIMITED")
            return type("R", (), {"returncode": 0})()

        with patch.object(driver, "_RESULTS_DIR", results_dir), \
             patch.object(driver, "_population_ids", return_value=["a", "b", "c", "d"]), \
             patch.object(driver.subprocess, "run", side_effect=fake_run), \
             patch.object(driver.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             patch.object(driver.config, "OPENBOOK_IMAGE", "fake-image"):
            driver.main(["--batch-size", "2", "--runs", "1",
                        "--base-backoff-s", "10", "--max-backoff-s", "40",
                        "--summary-out", str(summary_path)])

        assert sleeps == [10, 20], "backoff should double after each fully-rate-limited batch"
        state = json.loads(summary_path.read_text())
        assert state["batches_done"] == 2
        assert len(state["backoff_events"]) == 2


def test_main_does_not_back_off_when_a_batch_is_clean(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        results_dir = Path(tmp) / "results"
        summary_path = Path(tmp) / "summary.json"
        sleeps = []

        def fake_run(cmd, cwd=None):
            return type("R", (), {"returncode": 0})()  # no infra_error.json at all -> clean

        with patch.object(driver, "_RESULTS_DIR", results_dir), \
             patch.object(driver, "_population_ids", return_value=["a", "b"]), \
             patch.object(driver.subprocess, "run", side_effect=fake_run), \
             patch.object(driver.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             patch.object(driver.config, "OPENBOOK_IMAGE", "fake-image"):
            driver.main(["--batch-size", "2", "--runs", "1",
                        "--summary-out", str(summary_path)])

        assert sleeps == []


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t(None) if "monkeypatch" in t.__code__.co_varnames else t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
