"""Offline tests for Codex JSONL normalization and isolated MCP configuration."""
import json
import sys
import time

from core.codex_loop import build_codex_cmd, parse_codex_output, run_codex_meta


def test_build_codex_cmd_pins_model_and_isolates_user_config(tmp_path):
    cmd = build_codex_cmd(
        "do it", model="gpt-6-astra", cwd=tmp_path,
        controller_url="http://controller.test:5000", reasoning_effort="high",
        python_bin="/usr/bin/python3",
    )
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--model") + 1] == "gpt-6-astra"
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--sandbox" not in cmd
    joined = " ".join(cmd)
    assert "benchmarks.osworld.mcp.server" in joined
    assert "http://controller.test:5000" in joined
    assert "model_reasoning_effort=\"high\"" in cmd
    disabled = {cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "--disable"}
    assert {"shell_tool", "browser_use", "computer_use", "apps", "plugins",
            "multi_agent", "multi_agent_v2", "browser_use_external",
            "browser_use_full_cdp_access", "goals", "sleep_tool", "view_image",
            "unified_exec"} <= disabled
    assert "code_mode_host" not in disabled


def test_parse_codex_output_extracts_last_message_usage_and_thread():
    lines = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "working"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "name": "screenshot"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ANSWER: DONE"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}},
    ]
    meta = parse_codex_output("\n".join(json.dumps(x) for x in lines))
    assert meta["result"] == "ANSWER: DONE"
    assert meta["session_id"] == "thread-1"
    assert meta["usage"]["input_tokens"] == 12
    assert meta["tool_calls"] == 1
    assert meta["mcp_tool_calls"] == 1
    assert meta["non_mcp_tool_calls"] == 0
    assert meta["is_error"] is False


def test_parse_codex_output_deduplicates_started_and_completed_by_id():
    lines = [
        {"type": "item.started", "item": {"id": "call-1", "type": "mcp_tool_call",
                                            "tool": "screenshot"}},
        {"type": "item.completed", "item": {"id": "call-1", "type": "mcp_tool_call",
                                              "tool": "screenshot"}},
    ]
    meta = parse_codex_output("\n".join(json.dumps(x) for x in lines))
    assert meta["tool_calls"] == 1


def test_parse_codex_output_counts_repeated_idless_completed_calls():
    line = {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "click"}}
    meta = parse_codex_output("\n".join(json.dumps(line) for _ in range(2)))
    assert meta["tool_calls"] == 2


def test_parse_codex_output_flags_any_non_mcp_tool_family():
    lines = [
        {"type": "item.completed", "item": {"id": "x", "type": "command_execution"}},
        {"type": "item.completed", "item": {"id": "y", "type": "computer_call"}},
        {"type": "item.completed", "item": {"id": "z", "type": "file_change"}},
    ]
    meta = parse_codex_output("\n".join(json.dumps(x) for x in lines))
    assert meta["non_mcp_tool_calls"] == 3


def test_parse_codex_output_retains_malformed_line_as_error():
    meta = parse_codex_output('{"type":"thread.started","thread_id":"x"}\nnot-json',
                              returncode=1)
    assert meta["is_error"] is True
    assert meta["errors"][-1] == {"malformed_jsonl_lines": 1}


def test_parse_codex_output_treats_completed_error_item_as_failure():
    raw = json.dumps({"type": "item.completed", "item": {
        "id": "e", "type": "error", "message": "tool host unavailable"}})
    meta = parse_codex_output(raw)
    assert meta["is_error"] is True
    assert meta["errors"] == ["tool host unavailable"]


def test_run_codex_meta_kills_the_whole_process_group_on_timeout():
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print('{\"type\":\"thread.started\",\"thread_id\":\"partial\"}',flush=True); "
        "time.sleep(30)"
    )
    started = time.monotonic()
    meta = run_codex_meta([sys.executable, "-c", code], timeout=0.5)
    assert time.monotonic() - started < 5
    assert meta["timed_out"] is True
    assert meta["session_id"] == "partial"


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        if "tmp_path" in test.__code__.co_varnames[:test.__code__.co_argcount]:
            import tempfile
            from pathlib import Path
            test(Path(tempfile.mkdtemp(prefix="codex-loop-test-")))
        else:
            test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
