"""Pure regression tests for the GPT Astra runner (no desktop and no model call).

Several of these encode a mistake the Sonnet-5 campaign actually paid for, so the Astra
campaign cannot repeat it -- each such test names the incident it descends from.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from benchmarks.osworld import config
from benchmarks.osworld.runners import gpt_astra
from benchmarks.osworld.runners.gpt_astra import (
    _api_error_status, _estimated_api_cost, _provenance_astra, _telemetry,
    _validate_campaign_lock,
)
from core.codex_loop import session_context


def test_telemetry_reads_nested_codex_usage_and_labels_estimate():
    meta = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 12},
        },
        "tool_calls": 2, "mcp_tool_calls": 2, "non_mcp_tool_calls": 0,
    }
    rec = _telemetry(meta)
    assert rec["agent_cached_input_tokens"] == 40
    assert rec["agent_reasoning_output_tokens"] == 12
    assert rec["agent_cost_usd"] is None
    assert rec["agent_estimated_api_cost_usd"] == _estimated_api_cost(100, 40, 20)
    assert "list price" in rec["agent_cost_basis"]


def test_cost_estimate_does_not_double_charge_cached_tokens():
    assert _estimated_api_cost(1_000_000, 250_000, 100_000) == 12.75
    assert _estimated_api_cost(None, 0, 1) is None


def test_default_campaign_has_canonical_result_tree():
    assert config.ASTRA_MODEL == "gpt-6-astra"
    assert config.ASTRA_REASONING_EFFORT == "high"
    assert config.ASTRA_CODEX_VERSION == "0.153.4"
    assert config.ASTRA_SYSTEM_NAME == "agent_computer_astra"
    _validate_campaign_lock()


def test_campaign_overrides_cannot_pool_into_canonical_tree():
    original = (config.ASTRA_MODEL, config.ASTRA_REASONING_EFFORT,
                config.ASTRA_CODEX_VERSION)
    try:
        config.ASTRA_CODEX_VERSION = "different/version"
        name = config.astra_system_name()
    finally:
        (config.ASTRA_MODEL, config.ASTRA_REASONING_EFFORT,
         config.ASTRA_CODEX_VERSION) = original
    assert name != "agent_computer_astra"
    assert "/" not in name


def _rollout(root, session_id, *, model="gpt-6-astra", effort="high"):
    d = Path(root) / "2026" / "09" / "09"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-09-09T08-56-43-{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta",
                    "payload": {"session_id": session_id, "cli_version": "0.153.4",
                                "model_provider": "openai"}}) + "\n"
        + json.dumps({"type": "turn_context",
                      "payload": {"model": model, "effort": effort,
                                  "approval_policy": "never",
                                  "sandbox_policy": {"type": "danger-full-access"},
                                  "permission_profile": {"type": "disabled"}}}) + "\n")
    return path


def test_a_usage_limit_only_on_stderr_is_still_a_usage_limit():
    """Codex does not always put throttling in the JSONL error stream. Missing it would score a
    throttled run as if the agent had had its chance."""
    assert _api_error_status({"errors": None}, "stream error: usage limit reached") == 429
    assert _api_error_status({"errors": [{"message": "HTTP 429"}]}, "") == 429
    assert _api_error_status({"errors": [{"message": "file not found"}]}, "ok") is None


def test_the_served_model_is_read_back_from_codex_own_rollout():
    """`codex exec --json` names no model anywhere in its event stream (verified on the accepted
    canary), so --model is a request with no receipt. The Sonnet campaign ran across three
    models for weeks because nothing on disk recorded what actually served."""
    root = Path(tempfile.mkdtemp())
    _rollout(root, "sess-1")
    with patch("core.codex_loop.SESSIONS_ROOT", root):
        ctx = session_context("sess-1")
    assert ctx["model_served"] == "gpt-6-astra"
    assert ctx["reasoning_effort_served"] == "high"
    assert ctx["approval_policy_served"] == "never"
    assert ctx["sandbox_policy_served"] == "danger-full-access"


def test_provenance_flags_a_model_or_effort_substitution():
    root = Path(tempfile.mkdtemp())
    _rollout(root, "sess-2", model="gpt-5-mini", effort="low")
    task = {"id": "t", "instruction": "i", "evaluator": {}}
    with patch("core.codex_loop.SESSIONS_ROOT", root):
        rec = _provenance_astra(task, None, "2026-09-09T00:00:00Z", "codex-cli 0.153.4",
                                session_id="sess-2")
    assert rec["model_served"] == "gpt-5-mini"
    assert rec["model_mismatch"] is True
    assert rec["reasoning_effort_mismatch"] is True


def test_provenance_says_unknown_rather_than_guessing_when_no_rollout_exists():
    """Unverifiable must stay None: a provenance field that reports an intention is worse than
    an absent one."""
    root = Path(tempfile.mkdtemp())
    task = {"id": "t", "instruction": "i", "evaluator": {}}
    with patch("core.codex_loop.SESSIONS_ROOT", root):
        rec = _provenance_astra(task, None, "2026-09-09T00:00:00Z", "codex-cli 0.153.4",
                                session_id="absent")
    assert rec["model_served"] is None and rec["model_mismatch"] is None
    assert rec["model_requested"] == config.ASTRA_MODEL


def _run_with(meta, tmp):
    """Drive run() against a stubbed Codex, returning (result, eval, infra)."""
    out = Path(tmp)
    task = {"id": "t1", "instruction": "do it", "related_apps": ["libreoffice_calc"],
            "evaluator": {"func": "exact_match", "result": {"type": "vm_file"}}}
    with patch.object(gpt_astra, "run_codex_meta", return_value=dict(meta)), \
         patch.object(gpt_astra, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gpt_astra, "_score", return_value={"verdict": "FAILURE", "reward": 0.0}), \
         patch.object(gpt_astra, "_capture_eval_state", return_value=None):
        gpt_astra.run(task, env=type("E", (), {"browser": None, "setup_error": None})(), out=out)
    read = lambda n: json.loads((out / n).read_text()) if (out / n).exists() else None
    return read("result.json"), read("eval.json"), read("infra_error.json")


def _meta(**over):
    base = {"result": "ANSWER: DONE", "session_id": None, "usage": {}, "errors": None,
            "is_error": False, "timed_out": False, "returncode": 0, "num_turns": 1,
            "tool_calls": 3, "mcp_tool_calls": 3, "non_mcp_tool_calls": 0,
            "tool_names": ["screenshot"], "events": [], "raw": '{"type":"thread.started"}\n',
            "stderr": ""}
    base.update(over)
    return base


def test_a_run_throttled_after_the_agent_acted_is_never_scored():
    """Sonnet's 881deb30/run_3: 9 turns, then a 429, scored FAILURE. A verdict on a truncated
    agent measures the API's patience, not the agent."""
    result, verdict, infra = _run_with(
        _meta(mcp_tool_calls=7, errors=[{"message": "429 usage limit"}], is_error=True),
        tempfile.mkdtemp())
    assert verdict is None, "a throttled run must not produce a verdict"
    # infra_error.json accumulates one record per attempt (core.results.write_infra_error)
    assert infra and infra[-1]["outcome"] == "RATE_LIMITED"
    assert result["agent_api_error_status"] == 429


def test_transcript_flags_are_measured_not_asserted():
    empty, _, _ = _run_with(_meta(raw="", errors=[{"message": "429"}]), tempfile.mkdtemp())
    assert empty["transcript_saved"] is False and empty["transcript_bytes"] == 0
    full, _, _ = _run_with(_meta(), tempfile.mkdtemp())
    assert full["transcript_saved"] is True and full["transcript_bytes"] > 0


def test_a_clean_run_is_scored_and_keeps_its_eval_state_slot():
    result, verdict, infra = _run_with(_meta(), tempfile.mkdtemp())
    assert infra is None
    assert verdict["verdict"] == "FAILURE"
    assert "eval_state" in result and result["agent_clean_finish"] is True


def test_eval_state_capture_follows_official_scoring_postconfig():
    """A vm_file can be created by the evaluator's postconfig; capture it afterwards."""
    order = []
    task = {"id": "t1", "instruction": "do it", "related_apps": ["libreoffice_calc"],
            "evaluator": {"func": "exact_match", "result": {"type": "vm_file"}}}
    with patch.object(gpt_astra, "run_codex_meta", return_value=_meta()), \
         patch.object(gpt_astra, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gpt_astra, "_score", side_effect=lambda *a: (
             order.append("score") or {"verdict": "FAILURE", "reward": 0.0})), \
         patch.object(gpt_astra, "_capture_eval_state", side_effect=lambda *a: (
             order.append("capture") or "postconfig artifact")):
        gpt_astra.run(task, env=type("E", (), {
            "browser": type("C", (), {"base_url": "http://controller"})(), "setup_error": None
        })(),
                       out=Path(tempfile.mkdtemp()))
    assert order == ["score", "capture"]


def test_agent_declared_fail_does_not_fetch_an_uncreated_postconfig_file():
    task = {"id": "t1", "instruction": "do it", "related_apps": ["libreoffice_calc"],
            "evaluator": {"func": "exact_match", "result": {"type": "vm_file"}}}
    out = Path(tempfile.mkdtemp())
    with patch.object(gpt_astra, "run_codex_meta", return_value=_meta(result="ANSWER: FAIL")), \
         patch.object(gpt_astra, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gpt_astra, "_score", return_value={"verdict": "FAILURE", "reward": 0.0}), \
         patch.object(gpt_astra, "_capture_eval_state") as capture:
        gpt_astra.run(task, env=type("E", (), {
            "browser": type("C", (), {"base_url": "http://controller"})(), "setup_error": None
        })(), out=out)
    assert not capture.called
    assert json.loads((out / "result.json").read_text())["eval_state"]["capture_status"] == "not_applicable"


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
