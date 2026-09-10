"""Unit tests for the clean-vs-incidental-success, environment-error, run-telemetry and
gold-hashing logic in runners/agent_computer.py (pure, no desktop/LLM):
  python -m benchmarks.osworld.tests.test_runner
"""
import hashlib
import json
import tempfile
import time
from pathlib import Path

from benchmarks.osworld.runners import agent_computer
from benchmarks.osworld.runners.agent_computer import (
    _action_history, _agent_telemetry, _annotate_incidental, _bounded, _clean_finish,
    _environment_error_rec, _evaluator_provenance, _extra_flags, _implies_done, _inloop_verify,
    _mcp_config, _model_mismatch, _provenance, _rate_limit_infra_rec, _rate_limit_result_rec,
    _save_conversation_transcript, _score, _served_by,
)
from benchmarks.osworld import config


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


class _FakeCtrl:
    def __init__(self, png_bytes=b"\x89PNG\r\n\x1a\nfake"):
        self._png_bytes = png_bytes
        self.screenshot_calls = 0

    def screenshot(self):
        self.screenshot_calls += 1
        return self._png_bytes


def _with_patched(module, name, value, fn):
    real = getattr(module, name)
    setattr(module, name, value)
    try:
        return fn()
    finally:
        setattr(module, name, real)


def test_implies_done_true_for_a_plain_done():
    assert _implies_done("DONE") is True


def test_implies_done_false_for_fail_or_infeasible():
    assert _implies_done("FAIL") is False
    assert _implies_done("This task is INFEASIBLE") is False
    assert _implies_done("") is False


def test_inloop_verify_off_by_default_never_touches_the_desktop():
    """The whole mechanism is opt-in (config.INLOOP_VERIFY, default 0) -- with it off, not even
    a screenshot should be taken, so a normal campaign pays zero cost for the feature existing."""
    ctrl = _FakeCtrl()
    real = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = False
    try:
        answer, meta, telemetry = _inloop_verify(
            ctrl, {"instruction": "x"}, "DONE", {"session_id": "s1"}, "/tmp/mcp.json",
            Path(tempfile.mkdtemp(prefix="osw_il_")))
    finally:
        config.INLOOP_VERIFY = real
    assert answer == "DONE" and telemetry == {"inloop_verify_used": False}
    assert ctrl.screenshot_calls == 0


def test_inloop_verify_skips_a_self_reported_fail():
    """A FAIL is the agent's own admission -- no independent check needed to act on it."""
    ctrl = _FakeCtrl()
    real = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        answer, meta, telemetry = _inloop_verify(
            ctrl, {"instruction": "x"}, "FAIL", {"session_id": "s1"}, "/tmp/mcp.json",
            Path(tempfile.mkdtemp(prefix="osw_il_")))
    finally:
        config.INLOOP_VERIFY = real
    assert answer == "FAIL" and telemetry == {"inloop_verify_used": False}
    assert ctrl.screenshot_calls == 0


def test_inloop_verify_skips_an_excluded_app_bucket():
    """2026-09-10 finding: the 'os' bucket's tasks leave no GUI window open, so the in-loop
    screenshot came back an uninformative black screen on every one of that bucket's runs in the
    pilot -- config.INLOOP_VERIFY_SKIP_APPS lets that bucket skip the mechanism entirely rather
    than pay for a check that cannot possibly confirm anything."""
    ctrl = _FakeCtrl()
    real_flag, real_skip = config.INLOOP_VERIFY, config.INLOOP_VERIFY_SKIP_APPS
    config.INLOOP_VERIFY = True
    config.INLOOP_VERIFY_SKIP_APPS = {"os"}
    try:
        answer, meta, telemetry = _inloop_verify(
            ctrl, {"instruction": "x", "related_apps": ["os"]}, "DONE", {"session_id": "s1"},
            "/tmp/mcp.json", Path(tempfile.mkdtemp(prefix="osw_il_")))
    finally:
        config.INLOOP_VERIFY, config.INLOOP_VERIFY_SKIP_APPS = real_flag, real_skip
    assert answer == "DONE"
    assert telemetry == {"inloop_verify_used": False, "inloop_verify_skipped_app": True}
    assert ctrl.screenshot_calls == 0


def test_inloop_verify_agreement_skips_the_retry_call():
    """Verifier agrees with the agent's DONE -> no --resume call should ever be attempted."""
    ctrl = _FakeCtrl()
    out = Path(tempfile.mkdtemp(prefix="osw_il_"))
    resume_calls = []

    def fake_verify_with_reason(png_path, instruction, timeout=120):
        assert png_path.exists()
        return {"answer": "DONE", "reason": "the target file shows the expected text",
                "raw": "ANSWER: DONE\nREASON: x", "model_served": ["claude-sonnet-5"]}

    def fake_run_claude_meta(cmd, timeout=None):
        resume_calls.append(cmd)
        raise AssertionError("should not be called when the verifier agrees")

    real_verify_config = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        answer, meta, telemetry = _with_patched(
            agent_computer, "_verify_with_reason", fake_verify_with_reason, lambda:
            _with_patched(agent_computer, "run_claude_meta", fake_run_claude_meta, lambda:
                _inloop_verify(ctrl, {"instruction": "do the thing"}, "DONE",
                              {"session_id": "s1", "total_cost_usd": 0.10}, "/tmp/mcp.json", out)))
    finally:
        config.INLOOP_VERIFY = real_verify_config
    assert answer == "DONE"
    assert resume_calls == []
    assert telemetry["inloop_verify_used"] is True
    assert telemetry["inloop_verify_verdict"] == "DONE"
    assert telemetry["inloop_verify_retried"] is False
    assert ctrl.screenshot_calls == 1
    assert (out / "inloop_pre_verify.png").exists()


def test_inloop_verify_disagreement_retries_with_the_specific_reason():
    """Verifier disagrees with the agent's DONE -> a --resume call is made carrying the
    verifier's specific reason (not a generic 'check again'), its answer wins, and the two
    calls' cost/turns are summed rather than the first being silently dropped. Grounded in the
    2026-09-09 pilot: a generic nudge never once changed the agent's self-report (0/11) because
    a screenshot-only verifier is blind to non-visual state the agent can check with run_python;
    naming the specific claim is meant to close that escape hatch."""
    ctrl = _FakeCtrl()
    out = Path(tempfile.mkdtemp(prefix="osw_il_"))
    retry_prompts = []

    def fake_verify_with_reason(png_path, instruction, timeout=120):
        return {"answer": "FAIL", "reason": "the sidebar still shows the old filename",
                "raw": "ANSWER: FAIL\nREASON: x", "model_served": ["claude-sonnet-5"]}

    def fake_run_claude_meta(cmd, timeout=None):
        assert "--resume" in cmd and "s1" in cmd
        retry_prompts.append(cmd[cmd.index("-p") + 1])
        return {"result": "ANSWER: FAIL", "session_id": "s1", "total_cost_usd": 0.05,
                "num_turns": 3, "duration_ms": 1000, "duration_api_ms": 800,
                "modelUsage": {"claude-sonnet-5": {}}}

    real_verify_config = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        answer, meta, telemetry = _with_patched(
            agent_computer, "_verify_with_reason", fake_verify_with_reason, lambda:
            _with_patched(agent_computer, "run_claude_meta", fake_run_claude_meta, lambda:
                _inloop_verify(ctrl, {"instruction": "do the thing"}, "DONE",
                              {"session_id": "s1", "total_cost_usd": 0.10, "num_turns": 5,
                               "duration_ms": 2000, "duration_api_ms": 1500}, "/tmp/mcp.json", out)))
    finally:
        config.INLOOP_VERIFY = real_verify_config
    assert answer == "FAIL"
    assert telemetry["inloop_verify_retried"] is True
    assert telemetry["inloop_verify_second_answer"] == "FAIL"
    assert telemetry["inloop_verify_reason"] == "the sidebar still shows the old filename"
    assert "the sidebar still shows the old filename" in retry_prompts[0]
    assert round(meta["total_cost_usd"], 2) == 0.15
    assert meta["num_turns"] == 8
    assert meta["duration_ms"] == 3000


def test_inloop_verify_disagreement_without_a_parsed_reason_falls_back_to_generic_text():
    """A verifier reply that skips the REASON line must not crash the retry -- it falls back to
    a generic phrase rather than interpolating None into the prompt."""
    ctrl = _FakeCtrl()
    out = Path(tempfile.mkdtemp(prefix="osw_il_"))
    retry_prompts = []

    def fake_verify_with_reason(png_path, instruction, timeout=120):
        return {"answer": "FAIL", "reason": None, "raw": "ANSWER: FAIL",
                "model_served": ["claude-sonnet-5"]}

    def fake_run_claude_meta(cmd, timeout=None):
        retry_prompts.append(cmd[cmd.index("-p") + 1])
        return {"result": "ANSWER: DONE", "session_id": "s1"}

    real = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        _with_patched(
            agent_computer, "_verify_with_reason", fake_verify_with_reason, lambda:
            _with_patched(agent_computer, "run_claude_meta", fake_run_claude_meta, lambda:
                _inloop_verify(ctrl, {"instruction": "do the thing"}, "DONE",
                              {"session_id": "s1"}, "/tmp/mcp.json", out)))
    finally:
        config.INLOOP_VERIFY = real
    assert "None" not in retry_prompts[0]
    assert "does not appear to show the task as complete" in retry_prompts[0]


def test_inloop_verify_without_session_id_cannot_resume():
    ctrl = _FakeCtrl()
    out = Path(tempfile.mkdtemp(prefix="osw_il_"))

    def fake_verify_with_reason(png_path, instruction, timeout=120):
        return {"answer": "FAIL", "reason": None, "raw": "", "model_served": None}

    real = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        answer, meta, telemetry = _with_patched(
            agent_computer, "_verify_with_reason", fake_verify_with_reason, lambda:
            _inloop_verify(ctrl, {"instruction": "x"}, "DONE", {}, "/tmp/mcp.json", out))
    finally:
        config.INLOOP_VERIFY = real
    assert answer == "DONE" and telemetry["inloop_verify_retried"] is False
    assert "no session_id" in telemetry["inloop_verify_error"]


def test_inloop_verify_screenshot_failure_is_non_fatal():
    class _BrokenCtrl:
        def screenshot(self):
            raise RuntimeError("controller unreachable")

    out = Path(tempfile.mkdtemp(prefix="osw_il_"))
    real = config.INLOOP_VERIFY
    config.INLOOP_VERIFY = True
    try:
        answer, meta, telemetry = _inloop_verify(
            _BrokenCtrl(), {"instruction": "x"}, "DONE", {"session_id": "s1"},
            "/tmp/mcp.json", out)
    finally:
        config.INLOOP_VERIFY = real
    assert answer == "DONE" and telemetry["inloop_verify_used"] is False
    assert "controller unreachable" in telemetry["inloop_verify_error"]


def test_mcp_config_writes_a_valid_stdio_spec():
    """Characterization test (docs/verify-replan-minimal-integration-plan.md commit 1): pins
    the exact shape _mcp_config produces today, before it moves into a shared module. A
    verify-replan Auditor needs the same spec shape with a different tool allowlist -- this
    locks down the part that must stay identical."""
    path = _mcp_config("http://localhost:9999")
    try:
        spec = json.loads(Path(path).read_text())
        assert spec == {"mcpServers": {"osworld": {
            "type": "stdio", "command": "python",
            "args": ["-m", "benchmarks.osworld.mcp.server"],
            "env": {"OSW_CONTROLLER_URL": "http://localhost:9999"},
        }}}
    finally:
        Path(path).unlink()


def test_mcp_config_defaults_to_empty_url_env():
    path = _mcp_config(None)
    try:
        spec = json.loads(Path(path).read_text())
        assert spec["mcpServers"]["osworld"]["env"]["OSW_CONTROLLER_URL"] == ""
    finally:
        Path(path).unlink()


def test_extra_flags_none_when_no_restriction_is_active():
    real_a, real_b = config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON
    config.ENFORCE_SANDBOX = config.RESTRICT_RUN_PYTHON = False
    try:
        assert _extra_flags() is None
    finally:
        config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON = real_a, real_b


def test_extra_flags_combines_both_restrictions_with_one_strict_mcp_config():
    """--strict-mcp-config must appear at most once even with both G5 arms (#10 and #11)
    active together -- two separate flags would be a malformed argv the CLI rejects."""
    real_a, real_b = config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON
    config.ENFORCE_SANDBOX = config.RESTRICT_RUN_PYTHON = True
    try:
        flags = _extra_flags()
    finally:
        config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON = real_a, real_b
    assert flags == ["--disallowedTools", "Bash", "WebSearch", "WebFetch",
                     "mcp__osworld__run_python", "--strict-mcp-config"]
    assert flags.count("--strict-mcp-config") == 1


def test_action_history_maps_fail_and_infeasible_to_fail():
    assert _action_history("FAIL") == ["FAIL"]
    assert _action_history("This task is INFEASIBLE") == ["FAIL"]
    assert _action_history("DONE") == ["DONE"]


def test_bounded_returns_the_wrapped_function_result():
    assert _bounded("quick", lambda x: x * 2, 21) == 42


def test_bounded_raises_runtime_error_past_its_timeout():
    """The watchdog that stops a hung eval-state capture/score from stranding a whole run for
    an hour (see the module docstring above _bounded) -- verified here without actually
    waiting out the real 600s default by monkeypatching the module-level timeout constant."""
    real_timeout = agent_computer._POST_RUN_TIMEOUT_S
    agent_computer._POST_RUN_TIMEOUT_S = 0.05
    try:
        try:
            _bounded("slow", time.sleep, 5)
            raised = False
        except RuntimeError as e:
            raised = True
            assert "slow" in str(e) and "0.05" in str(e)
        assert raised
    finally:
        agent_computer._POST_RUN_TIMEOUT_S = real_timeout


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
    comes from (task hash, ctrl, or a specific config knob), so extracting this into a shared
    module cannot silently drop or rename a field a downstream reader (reporting.ab_compare,
    the verify-replan role telemetry) depends on."""
    real = {k: getattr(config, k) for k in
            ("MODEL", "IMAGE", "RELEASE", "MAX_TURNS", "TASK_TIMEOUT", "OBSERVATION",
             "ACTION_SPACE")}
    config.MODEL = "claude-sonnet-5"
    config.IMAGE = "test-image@sha256:deadbeef"
    config.RELEASE = "verified"
    config.MAX_TURNS = 150
    config.TASK_TIMEOUT = 3600
    config.OBSERVATION = "screenshot+a11y"
    config.ACTION_SPACE = "pyautogui"
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
    assert rec["max_turns"] == 150
    assert rec["task_timeout"] == 3600
    assert rec["observation"] == "screenshot+a11y"
    assert rec["action_space"] == "pyautogui"
    assert rec["started_at"] == "2026-09-10T00:00:00+00:00"
    assert "evaluator_commit" in rec and "evaluator_package" in rec
    assert "finished_at" in rec and rec["finished_at"] != rec["started_at"]


def test_provenance_reads_the_controller_url_off_ctrl_when_present():
    class _Ctrl:
        base_url = "http://ctrl:1234"
    rec = _provenance({"id": "t1"}, ctrl=_Ctrl(), started_at="x")
    assert rec["controller_url"] == "http://ctrl:1234"


class _FakeEnv:
    """Minimal stand-in for core.environment's context value: run() only ever reads
    .browser and .setup_error off it (never calls into it as a context manager itself --
    that happens one level up, in core.run)."""
    browser = None
    setup_error = None


def test_dry_run_argv_is_a_stable_snapshot_for_a_fixed_task_and_config():
    """The plan's own gate before any extraction (verify-replan-minimal-integration-plan.md
    §17): 'dimostrare che una run baseline dry-run produce lo stesso argv'. Captures every
    build_claude_cmd kwarg for a fixed task under fixed config, with _mcp_config's random
    tmpfile name replaced by a fixed stand-in -- so this test fails loudly if extracting
    _mcp_config/_extra_flags/build_claude_cmd's call site into runners/common.py changes so
    much as one flag, ordering, or default for the untouched baseline path."""
    calls = []
    fixed_mcp_path = tempfile.mkstemp(prefix="osw_fixed_mcp_")[1]

    def fake_mcp_config(controller_url):
        return fixed_mcp_path

    def fake_build_claude_cmd(prompt, **kwargs):
        calls.append({"prompt_nonempty": bool(prompt), **kwargs})
        return ["claude", "-p", "FAKE"]

    real_model, real_max_turns = config.MODEL, config.MAX_TURNS
    real_a, real_b = config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON
    config.MODEL, config.MAX_TURNS = "claude-sonnet-5", 150
    config.ENFORCE_SANDBOX = config.RESTRICT_RUN_PYTHON = False
    try:
        _with_patched(agent_computer, "_mcp_config", fake_mcp_config, lambda:
            _with_patched(agent_computer, "build_claude_cmd", fake_build_claude_cmd, lambda:
                agent_computer.run(
                    {"id": "t1", "instruction": "do the thing", "evaluator": {}},
                    env=_FakeEnv(), out=Path(tempfile.mkdtemp(prefix="osw_dry_")), dry=True)))
    finally:
        config.MODEL, config.MAX_TURNS = real_model, real_max_turns
        config.ENFORCE_SANDBOX, config.RESTRICT_RUN_PYTHON = real_a, real_b
        Path(fixed_mcp_path).unlink(missing_ok=True)
    assert len(calls) == 1
    assert calls[0] == {
        "prompt_nonempty": True,
        "model": "claude-sonnet-5",
        "max_turns": 150,
        "mcp_config": fixed_mcp_path,
        "allowed_tools": agent_computer.OSWORLD_TOOLS,
        "extra": None,
    }


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
