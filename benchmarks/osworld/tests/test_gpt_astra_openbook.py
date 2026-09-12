"""Pure regression tests for the open-book GPT Astra runner (no desktop, no model call, no
mitmproxy) -- same convention as tests/test_gpt_astra.py: patch.object on the runner's own
module-level names, a minimal fake env, read-back of the written artifacts.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from benchmarks.osworld import open_book_preflight
from benchmarks.osworld.openbook_proxy.bundle import MOCK_ACCESS_TOKEN
from benchmarks.osworld.runners import gpt_astra_openbook as gao

_TASK = {"id": "t1", "instruction": "do it", "related_apps": ["chrome"],
        "evaluator": {"func": "exact_match", "result": {"type": "vm_file"}}}


def _ready_check(bundle_dir="/tmp/does-not-need-to-exist", manifest_sha256="deadbeef"):
    return {"ready": True, "reason": None, "bundle_dir": bundle_dir,
           "manifest_sha256": manifest_sha256}


def _not_ready_check(reason):
    return {"ready": False, "reason": reason, "bundle_dir": None, "manifest_sha256": None}


def _meta(**over):
    base = {"result": "ANSWER: DONE", "session_id": None, "usage": {}, "errors": None,
            "is_error": False, "timed_out": False, "returncode": 0, "num_turns": 1,
            "tool_calls": 3, "mcp_tool_calls": 3, "non_mcp_tool_calls": 0,
            "tool_names": ["screenshot"], "events": [], "raw": '{"type":"thread.started"}\n',
            "stderr": ""}
    base.update(over)
    return base


def _run_with(meta, tmp, *, task_check=None, score=None):
    out = Path(tmp)
    task_check = task_check if task_check is not None else _ready_check()
    with patch.object(open_book_preflight, "task_check", return_value=task_check), \
         patch.object(gao, "run_codex_meta", return_value=dict(meta)) as run_meta, \
         patch.object(gao, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gao, "_score_openbook",
                      return_value=score or {"verdict": "FAILURE", "reward": 0.0}), \
         patch.object(gao, "_capture_eval_state", return_value=None):
        gao.run(_TASK, env=type("E", (), {"browser": None, "setup_error": None})(), out=out)
    read = lambda n: json.loads((out / n).read_text()) if (out / n).exists() else None
    return read("result.json"), read("eval.json"), read("infra_error.json"), run_meta


def test_a_closed_book_or_stale_task_never_reaches_codex():
    """require_open_book's own PreflightError, surfaced through task_check, must stop the run
    before any Codex process starts -- same discipline as gpt_astra's closed-book-only lock."""
    result, verdict, infra, run_meta = _run_with(
        _meta(), tempfile.mkdtemp(),
        task_check=_not_ready_check("NOT_OPEN_BOOK: task t1 is not in the frozen population"))
    assert not run_meta.called
    assert verdict["verdict"] == "ENVIRONMENT_ERROR"
    assert "NOT_OPEN_BOOK" in verdict["reason"]


def test_a_missing_fixture_bundle_produces_environment_error_and_invokes_no_codex():
    """Spec requirement: absent proxy/fixture/auth -> ENVIRONMENT_ERROR, zero Codex invocations
    -- the exact SNAPSHOT_PROXY_MISSING case docs/g_astra_open_book_runner_implementation.md
    names by name."""
    result, verdict, infra, run_meta = _run_with(
        _meta(), tempfile.mkdtemp(),
        task_check=_not_ready_check("SNAPSHOT_PROXY_MISSING: no fixture bundle"))
    assert not run_meta.called
    assert verdict["verdict"] == "ENVIRONMENT_ERROR"
    assert "SNAPSHOT_PROXY_MISSING" in verdict["reason"]


def test_a_run_throttled_after_the_agent_acted_is_never_scored():
    result, verdict, infra, _ = _run_with(
        _meta(mcp_tool_calls=7, errors=[{"message": "429 usage limit"}], is_error=True),
        tempfile.mkdtemp())
    assert verdict is None, "a throttled run must not produce a verdict"
    assert infra and infra[-1]["outcome"] == "RATE_LIMITED"
    assert result["agent_api_error_status"] == 429


def test_a_clean_run_is_scored_and_writes_network_provenance():
    result, verdict, infra, _ = _run_with(_meta(), tempfile.mkdtemp())
    assert infra is None
    assert verdict["verdict"] == "FAILURE"
    assert result["network_provenance"]["network_mode"] == "controlled_snapshot"
    assert result["network_provenance"]["snapshot_bundle_sha256"] == "deadbeef"


def test_preflight_json_is_written_even_on_a_clean_run():
    out = Path(tempfile.mkdtemp())
    _run_with(_meta(), out)
    preflight = json.loads((out / "preflight.json").read_text())
    assert preflight["ready"] is True


def test_redaction_scan_blocks_a_run_that_leaked_the_mock_drive_token():
    """The mock Drive OAuth token (drive_mock.py) must never reach an agent-visible artifact --
    a hit is ARTIFACT_REDACTION_ERROR, not a scored verdict, regardless of what the agent did."""
    result, verdict, infra, _ = _run_with(
        _meta(raw=f"leaked: {MOCK_ACCESS_TOKEN}"), tempfile.mkdtemp())
    assert verdict is None
    assert infra and infra[-1]["outcome"] == "ARTIFACT_REDACTION_ERROR"


def test_host_proxy_wraps_scoring_only_for_tasks_tagged_with_a_host_side_getter():
    """get_cloud_file/get_googledrive_file need the host-side proxy scoped around evaluation;
    the three CDP-driven getters (already covered by the guest proxy once Chrome is proxied)
    must not pay for a second proxy process at all."""
    out = Path(tempfile.mkdtemp())
    calls = []
    with patch.object(open_book_preflight, "task_check", return_value=_ready_check()), \
         patch.object(gao, "run_codex_meta", return_value=_meta()), \
         patch.object(gao, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gao, "_capture_eval_state", return_value=None), \
         patch.object(gao, "_proxy_tags_for", return_value={"external_live_getter"}), \
         patch("benchmarks.osworld.env.host_proxy.host_proxy") as host_proxy_cm, \
         patch("benchmarks.osworld.env.host_proxy.scoped_env") as scoped_env_cm, \
         patch("benchmarks.osworld.runners.gpt_astra_openbook._evaluate_with_retry",
              return_value=1.0), \
         patch("benchmarks.osworld.env.osworld_eval.hash_gold_artifacts", return_value={}):
        host_proxy_cm.return_value.__enter__.return_value = {"proxy_url": "http://x", "port": 1}
        scoped_env_cm.return_value.__enter__.return_value = None
        gao.run(_TASK, env=type("E", (), {
            "browser": type("C", (), {"base_url": "http://controller"})(), "setup_error": None
        })(), out=out)
    assert host_proxy_cm.called, "external_live_getter must route scoring through the host proxy"


def test_host_proxy_is_skipped_for_a_purely_cdp_driven_task():
    out = Path(tempfile.mkdtemp())
    with patch.object(open_book_preflight, "task_check", return_value=_ready_check()), \
         patch.object(gao, "run_codex_meta", return_value=_meta()), \
         patch.object(gao, "_codex_version", return_value="codex-cli 0.153.4"), \
         patch.object(gao, "_capture_eval_state", return_value=None), \
         patch.object(gao, "_proxy_tags_for", return_value={"cdp_driven_live_getter"}), \
         patch("benchmarks.osworld.env.host_proxy.host_proxy") as host_proxy_cm, \
         patch("benchmarks.osworld.runners.gpt_astra_openbook._evaluate_with_retry",
              return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.hash_gold_artifacts", return_value={}):
        gao.run(_TASK, env=type("E", (), {
            "browser": type("C", (), {"base_url": "http://controller"})(), "setup_error": None
        })(), out=out)
    assert not host_proxy_cm.called


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
