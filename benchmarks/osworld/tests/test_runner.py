"""Unit tests for the clean-vs-incidental-success, environment-error, run-telemetry and
gold-hashing logic in runners/agent_computer.py (pure, no desktop/LLM):
  python -m benchmarks.osworld.tests.test_runner
"""
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from benchmarks.osworld.runners import common
from benchmarks.osworld.runners.agent_computer import (
    _action_history, _agent_telemetry, _annotate_incidental, _bounded,
    _environment_error_rec, _evaluator_provenance,
    _mcp_config, _model_mismatch, _provenance, _rate_limit_infra_rec, _rate_limit_result_rec,
    _save_conversation_transcript, _score, _served_by,
)
from benchmarks.osworld import config


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
        assert rec["eval_artifacts"] == []   # no controller -> nothing was ever downloaded
    finally:
        shutil.rmtree(d)


class _FakeScoreCtrl:
    """Just enough of a Controller for _score's `url = ctrl.base_url if ctrl else ...` line."""
    def __init__(self, base_url="http://fake-controller"):
        self.base_url = base_url


def test_collect_eval_artifacts_copies_files_with_correct_manifest():
    """Task 11c requirement 1+3: the files the getters actually placed in the scoring cache
    dir end up under <run dir>/eval_artifacts/, with a manifest entry per file."""
    from benchmarks.osworld.runners.common import _collect_eval_artifacts
    cache = Path(tempfile.mkdtemp(prefix="osw_test_cache_"))
    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    try:
        data = b"the file the evaluator actually compared"
        (cache / "result.docx").write_bytes(data)
        manifest = _collect_eval_artifacts(str(cache), out, {})
        assert len(manifest) == 1
        entry = manifest[0]
        assert entry["name"] == "result.docx"
        assert entry["kept"] is True
        assert entry["size"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        copied = out / "eval_artifacts" / "result.docx"
        assert copied.read_bytes() == data
    finally:
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_collect_eval_artifacts_empty_cache_dir_is_empty_list():
    from benchmarks.osworld.runners.common import _collect_eval_artifacts
    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    try:
        assert _collect_eval_artifacts("/nonexistent/cache/dir", out, {}) == []
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_collect_eval_artifacts_skips_oversized_file_but_lists_it():
    """Requirement 2: a single file over the 50MB cap is not copied, but still shows up with
    its size and sha256 so it's identifiable."""
    from benchmarks.osworld.runners import common
    from benchmarks.osworld.runners.common import _collect_eval_artifacts
    cache = Path(tempfile.mkdtemp(prefix="osw_test_cache_"))
    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    orig_cap = common._EVAL_ARTIFACT_MAX_FILE_BYTES
    try:
        common._EVAL_ARTIFACT_MAX_FILE_BYTES = 3   # small cap -> real bytes trip it, no mocking
        big = cache / "huge.bin"
        big.write_bytes(b"0123456789")
        manifest = _collect_eval_artifacts(str(cache), out, {})
        assert len(manifest) == 1
        assert manifest[0]["kept"] is False
        assert manifest[0]["size"] == 10
        assert manifest[0]["sha256"] == hashlib.sha256(b"0123456789").hexdigest()
        assert not (out / "eval_artifacts" / "huge.bin").exists()
    finally:
        common._EVAL_ARTIFACT_MAX_FILE_BYTES = orig_cap
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_collect_eval_artifacts_enforces_total_cap():
    """Requirement 2: once the running total would exceed the per-run cap, later files are
    listed but not copied, even though each is individually under the single-file cap."""
    from benchmarks.osworld.runners import common
    from benchmarks.osworld.runners.common import _collect_eval_artifacts
    cache = Path(tempfile.mkdtemp(prefix="osw_test_cache_"))
    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    orig_cap = common._EVAL_ARTIFACT_MAX_TOTAL_BYTES
    try:
        common._EVAL_ARTIFACT_MAX_TOTAL_BYTES = 10
        (cache / "a.bin").write_bytes(b"1234567890")   # exactly at cap -> kept
        (cache / "b.bin").write_bytes(b"1")             # would push over -> not kept
        manifest = {e["name"]: e for e in _collect_eval_artifacts(str(cache), out, {})}
        assert manifest["a.bin"]["kept"] is True
        assert manifest["b.bin"]["kept"] is False
        assert (out / "eval_artifacts" / "a.bin").exists()
        assert not (out / "eval_artifacts" / "b.bin").exists()
    finally:
        common._EVAL_ARTIFACT_MAX_TOTAL_BYTES = orig_cap
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_collect_eval_artifacts_copy_error_recorded_not_raised():
    """Requirement 3: when the copy itself fails, the error is recorded in the manifest, never
    raised -- diagnostics must never crash a run."""
    from benchmarks.osworld.runners import common
    from benchmarks.osworld.runners.common import _collect_eval_artifacts
    cache = Path(tempfile.mkdtemp(prefix="osw_test_cache_"))
    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    try:
        (cache / "result.bin").write_bytes(b"data")
        with pytest.MonkeyPatch.context() as mp:
            def boom(*a, **kw):
                raise OSError("disk full")
            mp.setattr(common.shutil, "copyfile", boom)
            manifest = _collect_eval_artifacts(str(cache), out, {})   # must not raise
        assert len(manifest) == 1
        assert manifest[0]["kept"] is False
        assert "error" in manifest[0] and "disk full" in manifest[0]["error"]
    finally:
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_score_populates_eval_artifacts_manifest_on_success():
    """Requirement 4: both arms get this through common._score. A fake evaluator that writes
    into the cache dir it's handed must show up in _score's returned eval_artifacts."""
    from benchmarks.osworld.runners import common

    def fake_evaluate_with_retry(url, task, action_history, cache_dir, **kw):
        (Path(cache_dir) / "downloaded_gold.bin").write_bytes(b"gold bytes")
        return 1.0

    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(common, "_evaluate_with_retry", fake_evaluate_with_retry)
            task = {"evaluator": {"func": "exact_match"}}
            rec = common._score(_FakeScoreCtrl(), task, "answer", out)
        assert rec["verdict"] == "SUCCESS"
        names = {e["name"] for e in rec["eval_artifacts"]}
        assert names == {"downloaded_gold.bin"}
        assert (out / "eval_artifacts" / "downloaded_gold.bin").read_bytes() == b"gold bytes"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_score_keeps_eval_artifacts_when_evaluator_raises_after_writing_a_file():
    """Requirement 5: scoring can fail AFTER some getters already ran (found live -- see the
    brief). The files that were written must still be captured, and the verdict path (offline
    fallback, EVAL_ERROR) must be exactly what it was before this change."""
    from benchmarks.osworld.runners import common

    def fake_evaluate_with_retry(url, task, action_history, cache_dir, **kw):
        (Path(cache_dir) / "partial_result.bin").write_bytes(b"partial")
        raise RuntimeError("getter blew up mid-scoring")

    out = Path(tempfile.mkdtemp(prefix="osw_test_out_"))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(common, "_evaluate_with_retry", fake_evaluate_with_retry)
            task = {"evaluator": {"func": "exact_match", "expected": {"rules": "x"}}}
            rec = common._score(_FakeScoreCtrl(), task, "answer", out)
        assert rec["verdict"] == "EVAL_ERROR"
        assert rec["source"] == "offline_fallback"
        names = {e["name"] for e in rec["eval_artifacts"]}
        assert names == {"partial_result.bin"}
    finally:
        shutil.rmtree(out, ignore_errors=True)


_RATE_LIMITED_TASK = {"id": "t1", "instruction": "do the thing",
                      "related_apps": ["libreoffice_calc"]}
_RATE_LIMITED_META = {"api_error_status": 429, "num_turns": 1, "is_error": True,
                      "total_cost_usd": 0, "usage": {}, "result": ""}


def test_rate_limit_result_rec_is_not_clean_finish():
    """A run the CLI never attempted (session limit hit before any turn) must never read as
    a clean finish -- that flag feeds _annotate_incidental's incidental-success detection."""
    rec = _rate_limit_result_rec(_RATE_LIMITED_TASK, _RATE_LIMITED_META)
    assert rec["agent_clean_finish"] is False
    assert rec["answer"] == ""
    assert rec["bucket"] == "libreoffice_calc"
    assert rec["agent_cost_usd"] == 0


def test_rate_limit_infra_rec_carries_the_status_code():
    rec = _rate_limit_infra_rec(_RATE_LIMITED_TASK, 429)
    assert rec["outcome"] == "RATE_LIMITED"
    assert rec["id"] == "t1"
    assert "429" in rec["error"]


def test_transcript_status_reports_missing_session_id():
    """No session_id -> report it instead of returning silently. Was a bare `return`."""
    st = _save_conversation_transcript({}, Path(tempfile.mkdtemp(prefix="osw_t_")), "task-x")
    assert st["transcript_saved"] is False
    assert "session" in st["transcript_error"]


def test_transcript_status_reports_missing_file():
    """A session_id with no transcript on disk (rotated / unexpected path) must be visible:
    this is the case that silently produced unverifiable runs before 2026-09-04."""
    st = _save_conversation_transcript(
        {"session_id": "definitely-not-a-real-session-id"},
        Path(tempfile.mkdtemp(prefix="osw_t_")), "task-x")
    assert st["transcript_saved"] is False
    assert "definitely-not-a-real-session-id" in st["transcript_error"]


def test_transcript_status_on_success(monkeypatch=None):
    """Happy path records the byte size, so a stub is distinguishable from a real capture."""
    home = Path(tempfile.mkdtemp(prefix="osw_home_"))
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "sess123.jsonl").write_text('{"type":"x"}\n')
    out = Path(tempfile.mkdtemp(prefix="osw_t_"))
    real_home = Path.home
    Path.home = staticmethod(lambda: home)
    try:
        st = _save_conversation_transcript({"session_id": "sess123"}, out, "task-x")
    finally:
        Path.home = real_home
    assert st["transcript_saved"] is True
    assert st["transcript_bytes"] == len('{"type":"x"}\n')
    assert (out / "conversation.jsonl").exists()


def test_served_by_ignores_the_cli_auxiliary_haiku():
    """Claude Code bills a few hundred Haiku tokens per session for its own internal work; it
    never drives the agent, so it must not be mistaken for the model under test."""
    meta = {"modelUsage": {"claude-sonnet-5": {"inputTokens": 100},
                           "claude-haiku-4-5-20251001": {"inputTokens": 881}}}
    assert _served_by(meta) == ["claude-sonnet-5"]


def test_model_mismatch_flags_a_run_served_by_the_wrong_model():
    """The check that was missing for the whole G3 campaign: three models' runs landed in one
    results tree with nothing on disk to tell them apart."""
    real = config.MODEL
    config.MODEL = "claude-sonnet-5"
    try:
        ok = _model_mismatch({"modelUsage": {"claude-sonnet-5": {}}})
        bad = _model_mismatch({"modelUsage": {"claude-sonnet-4-6": {}}})
    finally:
        config.MODEL = real
    assert ok["model_mismatch"] is False and ok["model_pinned"] is True
    assert bad["model_mismatch"] is True
    assert bad["model_served"] == ["claude-sonnet-4-6"]


def test_model_mismatch_is_none_when_nothing_was_pinned():
    """Unpinned is not a mismatch -- nothing was promised -- but it is still recorded, so a run
    can never again be silently assumed to be on the intended model."""
    real = config.MODEL
    config.MODEL = ""
    try:
        rec = _model_mismatch({"modelUsage": {"claude-sonnet-4-6": {}}})
    finally:
        config.MODEL = real
    assert rec["model_pinned"] is False and rec["model_mismatch"] is None
    assert rec["model_served"] == ["claude-sonnet-4-6"]


def test_mcp_config_writes_a_valid_stdio_spec():
    """Characterization test: pins the exact shape _mcp_config produces -- always the official
    shape now (this interpreter, PYTHONPATH set) since the runner has only one protocol."""
    import sys
    path = _mcp_config("http://localhost:9999")
    try:
        spec = json.loads(Path(path).read_text())
        assert spec["mcpServers"]["osworld"] == {
            "type": "stdio", "command": sys.executable,
            "args": ["-m", "benchmarks.osworld.mcp.server"],
            "env": {"OSW_CONTROLLER_URL": "http://localhost:9999",
                    "OSW_PROTOCOL": "official", "OSW_MAX_STEPS": str(config.MAX_STEPS),
                    "OSW_SLEEP_AFTER_EXECUTION": str(config.SLEEP_AFTER_EXECUTION),
                    "OSW_SCREEN_WIDTH": str(config.SCREEN_WIDTH),
                    "OSW_SCREEN_HEIGHT": str(config.SCREEN_HEIGHT),
                    "PYTHONPATH": str(common.CHECKOUT_ROOT)},
        }
    finally:
        Path(path).unlink()


def test_mcp_config_defaults_to_empty_url_env():
    path = _mcp_config(None)
    try:
        spec = json.loads(Path(path).read_text())
        assert spec["mcpServers"]["osworld"]["env"]["OSW_CONTROLLER_URL"] == ""
    finally:
        Path(path).unlink()


def test_action_history_maps_fail_and_infeasible_to_fail():
    assert _action_history("FAIL") == ["FAIL"]
    assert _action_history("This task is INFEASIBLE") == ["FAIL"]
    assert _action_history("DONE") == ["DONE"]


def test_bounded_returns_the_wrapped_function_result():
    assert _bounded("quick", lambda x: x * 2, 21) == 42


def test_bounded_raises_runtime_error_past_its_timeout():
    """The watchdog that stops a hung eval-state capture/score from stranding a whole run for
    an hour (see the module docstring above _bounded) -- verified here without actually
    waiting out the real 600s default by monkeypatching the timeout constant. Patched on
    `common` (where _bounded is now defined, post-extraction), not on the `agent_computer` name
    that merely re-imports it -- a function reads module globals off its own __globals__, i.e.
    the module it was DEFINED in, so patching the importer's copy of the name has no effect on
    what the function actually sees. Caught this exact gap live while verifying the extraction
    (docs/verify-replan-minimal-integration-plan.md commit 2): before this fix, this test's
    patch on `agent_computer._POST_RUN_TIMEOUT_S` silently did nothing post-extraction and
    `_bounded` ran out its real 600s default instead of the intended 0.05s."""
    real_timeout = common._POST_RUN_TIMEOUT_S
    common._POST_RUN_TIMEOUT_S = 0.05
    try:
        try:
            _bounded("slow", time.sleep, 5)
            raised = False
        except RuntimeError as e:
            raised = True
            assert "slow" in str(e) and "0.05" in str(e)
        assert raised
    finally:
        common._POST_RUN_TIMEOUT_S = real_timeout


def test_evaluator_provenance_falls_back_when_the_evaluator_package_is_unavailable():
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocked_import(name, *a, **k):
        if name == "benchmarks.osworld.env.osworld_eval":
            raise ImportError("simulated: desktop_env not installed")
        return real_import(name, *a, **k)

    import builtins
    real = builtins.__import__
    builtins.__import__ = blocked_import
    try:
        rec = _evaluator_provenance()
    finally:
        builtins.__import__ = real
    assert rec == {"evaluator_commit": None, "evaluator_package": None}


def test_provenance_shape_and_pinned_config_fields():
    """Characterization test: pins every field _provenance writes today and where each one
    comes from (task hash, ctrl, or a specific config knob, or the official protocol's own
    fixed values), so this cannot silently drop or rename a field a downstream reader
    (reporting.ab_compare, the verify-replan role telemetry) depends on."""
    real = {k: getattr(config, k) for k in ("MODEL", "IMAGE", "RELEASE", "TASK_TIMEOUT")}
    config.MODEL = "claude-sonnet-5"
    config.IMAGE = "test-image@sha256:deadbeef"
    config.RELEASE = "verified"
    config.TASK_TIMEOUT = 3600
    try:
        task = {"id": "t1", "instruction": "do the thing"}
        rec = _provenance(task, ctrl=None, started_at="2026-09-10T00:00:00+00:00")
    finally:
        for k, v in real.items():
            setattr(config, k, v)
    assert rec["task_sha256"] == hashlib.sha256(
        json.dumps(task, sort_keys=True).encode()).hexdigest()
    assert rec["image"] == "test-image@sha256:deadbeef"
    assert rec["model_requested"] == "claude-sonnet-5"
    assert rec["controller_url"] is None   # ctrl=None and config.CONTROLLER_URL unset in tests
    assert rec["release"] == "verified"
    # official protocol only: the model sees screenshots only, through computer_20251124.
    assert rec["protocol"] == "official"
    assert rec["max_turns"] == common.official_max_turns()
    assert rec["task_timeout"] == 3600
    assert rec["observation"] == "screenshot"
    assert rec["action_space"] == "computer_20251124"
    assert rec["started_at"] == "2026-09-10T00:00:00+00:00"
    assert "evaluator_commit" in rec and "evaluator_package" in rec
    assert "finished_at" in rec and rec["finished_at"] != rec["started_at"]


def test_provenance_reads_the_controller_url_off_ctrl_when_present():
    class _Ctrl:
        base_url = "http://ctrl:1234"
    rec = _provenance({"id": "t1"}, ctrl=_Ctrl(), started_at="x")
    assert rec["controller_url"] == "http://ctrl:1234"


# --- accessibility-channel probe ------------------

class _A11yCtrl:
    base_url = "http://guest:5000"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def a11y_tree(self):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


_EMPTY_AT = '{"AT": "<desktop-frame/>"}'
_POPULATED_AT = (
    '<desktop xmlns:st="https://accessibility.ubuntu.example.org/ns/state"'
    ' xmlns:cp="https://accessibility.ubuntu.example.org/ns/component">'
    '<push-button name="OK" st:showing="true" st:visible="true" st:enabled="true"'
    ' cp:screencoord="(10, 10)" cp:size="(20, 20)"/></desktop>'
)


def test_a11y_health_records_the_channel_state_without_raising():
    ok = common._a11y_health(_A11yCtrl(_POPULATED_AT))
    assert ok["a11y_ok"] is True and ok["a11y_elements"] == 1

    dead = common._a11y_health(_A11yCtrl(_EMPTY_AT))
    assert dead["a11y_ok"] is False and dead["a11y_nodes"] == 0
    assert "AT-SPI bridge" in dead["a11y_reason"]

    unreachable = common._a11y_health(_A11yCtrl(ConnectionError("down")))
    assert unreachable["a11y_ok"] is False
    assert "ConnectionError" in unreachable["a11y_reason"]

    assert common._a11y_health(None)["a11y_ok"] is None


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()


def test_collect_eval_artifacts_without_a_run_dir_is_a_noop(tmp_path):
    """Diagnostics must never break scoring: a caller without a run dir (out=None) gets []."""
    (tmp_path / "f.bin").write_bytes(b"x")
    assert common._collect_eval_artifacts(tmp_path, None, {"id": "t", "evaluator": {}}) == []
