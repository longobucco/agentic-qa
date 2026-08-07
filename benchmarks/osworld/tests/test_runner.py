"""Unit tests for the clean-vs-incidental-success, environment-error, run-telemetry and
gold-hashing logic in runners/agent_computer.py (pure, no desktop/LLM):
  python -m benchmarks.osworld.tests.test_runner
"""
from benchmarks.osworld.runners.agent_computer import (
    _agent_telemetry, _annotate_incidental, _clean_finish, _environment_error_rec, _score,
)


def test_clean_finish_true_on_normal_stop():
    assert _clean_finish({"is_error": False}, "some answer") is True


def test_clean_finish_false_without_answer():
    assert _clean_finish({"is_error": False}, "") is False


def test_clean_finish_false_on_error():
    assert _clean_finish({"is_error": True}, "some answer") is False


def test_clean_finish_false_on_max_turns_no_answer():
    meta = {"subtype": "error_max_turns", "is_error": True, "stop_reason": "tool_use"}
    assert _clean_finish(meta, "") is False


def test_annotate_incidental_flags_dirty_success():
    rec = _annotate_incidental({"verdict": "SUCCESS", "reward": 1.0}, clean_finish=False)
    assert "note" in rec and "incidental" in rec["note"]


def test_annotate_incidental_leaves_clean_success_untouched():
    rec = {"verdict": "SUCCESS", "reward": 1.0}
    assert _annotate_incidental(rec, clean_finish=True) == rec


def test_annotate_incidental_leaves_failure_untouched():
    rec = {"verdict": "FAILURE", "reward": 0.0}
    assert _annotate_incidental(rec, clean_finish=False) == rec


def test_environment_error_rec_shape():
    task = {"id": "t1", "instruction": "do the thing", "related_apps": ["libreoffice_calc"]}
    rec = _environment_error_rec(task, "config setup failed: 500 on /setup/open_file")
    assert rec["eval"]["verdict"] == "ENVIRONMENT_ERROR"
    assert rec["eval"]["id"] == "t1"
    assert "500" in rec["eval"]["reason"]
    assert rec["result"]["answer"] == ""
    assert rec["result"]["bucket"] == "libreoffice_calc"


def test_agent_telemetry_keeps_cost_and_tokens():
    """Cost/token fields must survive capture -- checked against a real `claude -p` envelope."""
    meta = {"num_turns": 3, "total_cost_usd": 0.42, "duration_ms": 1384,
            "session_id": "abc", "usage": {"input_tokens": 10, "output_tokens": 5,
                                            "cache_read_input_tokens": 7,
                                            "cache_creation_input_tokens": 9}}
    t = _agent_telemetry(meta)
    assert t["agent_cost_usd"] == 0.42
    assert t["agent_input_tokens"] == 10 and t["agent_output_tokens"] == 5
    assert t["agent_cache_read_tokens"] == 7 and t["agent_cache_creation_tokens"] == 9
    assert t["agent_duration_ms"] == 1384 and t["agent_session_id"] == "abc"


def test_agent_telemetry_survives_empty_envelope():
    """run_claude_meta returns {} when the output isn't the expected JSON envelope; telemetry
    must degrade to None rather than raise, or a malformed run would lose its whole record."""
    t = _agent_telemetry({})
    assert t["agent_cost_usd"] is None and t["agent_input_tokens"] is None


_CLOUD_FILE_TASK = {"evaluator": {"expected": {"type": "cloud_file", "dest": "gold.bin"}}}
_HERMETIC_TASK = {"evaluator": {"expected": {"type": "rule", "rules": {"key": "x"}},
                                "result": {"type": "gimp_config_file", "dest": "gimprc"}}}


def test_hash_gold_artifacts_detects_change():
    """A constant hash is what licenses keeping a web-dependent task in a reliability estimate;
    a changed one is ORACLE_DRIFT. Both directions must actually be detected."""
    import shutil
    import tempfile
    from pathlib import Path
    from benchmarks.osworld.env.osworld_eval import hash_gold_artifacts
    d = Path(tempfile.mkdtemp())
    try:
        (d / "gold.bin").write_bytes(b"v1")
        first = hash_gold_artifacts(str(d), _CLOUD_FILE_TASK)
        assert first == {"gold.bin": hash_gold_artifacts(str(d), _CLOUD_FILE_TASK)["gold.bin"]}
        assert hash_gold_artifacts(str(d), _CLOUD_FILE_TASK) == first   # same bytes -> same hash
        (d / "gold.bin").write_bytes(b"v2")
        assert hash_gold_artifacts(str(d), _CLOUD_FILE_TASK) != first   # changed bytes -> visible
    finally:
        shutil.rmtree(d)
    # no gold dir on disk at all -> empty, no raise
    assert hash_gold_artifacts("/nonexistent/path", _CLOUD_FILE_TASK) == {}


def test_hash_gold_artifacts_ignores_agent_result_not_just_missing_dir():
    """Regression for a real bug: get_vm_file (RESULT-side) writes into the same cache_dir as
    get_cloud_file (EXPECTED-side gold), so a hermetic task's agent-produced file was getting
    hashed as "gold drift" every run. Must return {} even though the dir is non-empty."""
    import shutil
    import tempfile
    from pathlib import Path
    from benchmarks.osworld.env.osworld_eval import hash_gold_artifacts
    d = Path(tempfile.mkdtemp())
    try:
        (d / "gimprc").write_bytes(b"agent's own produced config, changes every run")
        assert hash_gold_artifacts(str(d), _HERMETIC_TASK) == {}
    finally:
        shutil.rmtree(d)


def test_score_falls_back_to_eval_error_without_a_controller():
    """No official evaluator reachable (ctrl=None, no CONTROLLER_URL) -> the offline fallback
    path, which always reports EVAL_ERROR (see evaluate.py) -- never a guessed verdict."""
    import json
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="osw_test_"))
    try:
        (d / "result.json").write_text(json.dumps({"eval_state": "Budget"}))
        task = {"evaluator": {"func": "exact_match", "expected": {"rules": "Budget"}}}
        rec = _score(None, task, "Budget", d)
        assert rec["verdict"] == "EVAL_ERROR"
        assert rec["source"] == "offline_fallback"
    finally:
        shutil.rmtree(d)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
