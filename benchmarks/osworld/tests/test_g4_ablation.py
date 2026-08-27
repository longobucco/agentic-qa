"""Unit tests for g4_ablation.py (offline, no sandbox, synthetic raw data):
  python -m benchmarks.osworld.tests.test_g4_ablation
"""
import json
import tempfile
from pathlib import Path

from benchmarks.osworld.analysis import g4_ablation as g4
from benchmarks.osworld.analysis.g3_sample import sample


def _write_run(base, task_id, run_idx, verdict):
    d = base / "agent_computer" / task_id / f"run_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "eval.json").write_text(json.dumps({"id": task_id, "verdict": verdict}))


def test_scissor_returns_well_formed_report():
    r = g4.scissor()
    for k in ("n_tasks", "n_population_tasks", "n_runs_scored", "n_success",
              "n_environment_error", "n_eval_error_dropped"):
        assert k in r
    assert r["n_tasks"] <= r["n_population_tasks"]


def test_eval_error_excluded_from_both_conventions():
    """EVAL_ERROR must not enter the numerator or either denominator -- it's our own scoring
    flakiness, not the ENVIRONMENT_ERROR question G4 asks. A regression here would silently
    blend two different validity threats into one published number."""
    sample_ids = sample()
    tid = sample_ids[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, tid, 1, "SUCCESS")
    _write_run(d, tid, 2, "EVAL_ERROR")
    r = g4.scissor(results_dir=d)
    assert r["n_runs_scored"] == 1          # EVAL_ERROR run not counted as scored
    assert r["n_eval_error_dropped"] == 1
    assert r["excluding_env_errors"] == 1.0
    assert r["counting_env_as_failure"] == 1.0


def test_environment_error_only_moves_the_counting_convention():
    """The whole point of the ablation: ENVIRONMENT_ERROR runs are dropped from the
    excluding-convention denominator but counted as FAILURE (not dropped) under the
    counting-as-failure convention -- that asymmetry IS the scissor."""
    sample_ids = sample()
    tid = sample_ids[0]
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, tid, 1, "SUCCESS")
    _write_run(d, tid, 2, "ENVIRONMENT_ERROR")
    r = g4.scissor(results_dir=d)
    assert r["n_environment_error"] == 1
    assert r["excluding_env_errors"] == 1.0     # 1 success / 1 non-env run
    assert r["counting_env_as_failure"] == 0.5  # 1 success / 2 runs (env counted as failure)
    assert r["scissor_pp"] == 50.0


def test_scoped_to_current_sample_ignores_other_task_dirs():
    """results/agent_computer/ also holds one-off G0/G0.5 probe runs and pre-sample legacy
    pilots that were never part of the pre-registered G3 stratum -- including them would
    silently inflate n with data outside the frozen sample (plan §5)."""
    sample_ids = sample()
    tid = sample_ids[0]
    not_in_sample = "00000000-0000-0000-0000-000000000000"
    assert not_in_sample not in sample_ids
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, tid, 1, "SUCCESS")
    _write_run(d, not_in_sample, 1, "FAILURE")
    r = g4.scissor(results_dir=d)
    assert r["n_tasks"] == 1
    assert r["n_runs_scored"] == 1


def test_scissor_full_covers_tasks_outside_the_pre_registered_sample():
    """The 2026-08-25 extension: scissor_full() must see a task that scissor() (scoped to
    g3_sample.sample()) deliberately excludes -- otherwise it's not actually a wider scope,
    just the same one renamed. Uses a real runnable-but-not-in-sample id rather than a fake
    one, since scissor_full() filters by benchmarks.osworld.tasks.load_tasks(), not a
    synthetic allowlist."""
    from benchmarks.osworld import tasks as tasks_mod
    sample_ids = set(sample())
    runnable_ids = [t["id"] for t in tasks_mod.load_tasks()]
    not_in_sample = next(tid for tid in runnable_ids if tid not in sample_ids)
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    _write_run(d, not_in_sample, 1, "SUCCESS")
    r_sample = g4.scissor(results_dir=d)
    r_full = g4.scissor_full(results_dir=d)
    assert r_sample["n_tasks"] == 0          # invisible to the pre-registered scope
    assert r_full["n_tasks"] == 1            # visible to the full-population scope
    assert r_full["n_population_tasks"] == len(runnable_ids)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
