import json
from unittest.mock import patch

from benchmarks.osworld import config
from benchmarks.osworld.runners import agent_computer, common
from core.agent_loop import build_claude_cmd


class _FakeEnv:
    browser = None
    setup_error = None


def test_transcript_actions_extracts_text_and_computer_inputs(tmp_path):
    p = tmp_path / "c.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "name": "mcp__osworld__computer",
             "input": {"action": "left_click", "coordinate": [1, 2]}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "[INFEASIBLE]"}]}},
        {"type": "user", "message": {"content": "ignored"}},
    ]
    p.write_text("\n".join(json.dumps(l) for l in lines))
    texts, calls = common.claude_transcript_actions(p)
    assert texts == ["looking", "[INFEASIBLE]"]
    assert calls == [{"action": "left_click", "coordinate": [1, 2]}]


def test_transcript_actions_skips_garbage_and_string_content(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join([
        "not json",
        json.dumps({"type": "assistant", "message": {"content": "plain string"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__osworld__other", "input": {"x": 1}}]}}),
        "",
    ]))
    texts, calls = common.claude_transcript_actions(p)
    assert texts == ["plain string"]
    assert calls == []


def test_protocol_wait_only_under_official(monkeypatch):
    slept = []
    monkeypatch.setattr(config, "OFFICIAL", False)
    common.protocol_wait(60, sleep=slept.append)
    monkeypatch.setattr(config, "OFFICIAL", True)
    common.protocol_wait(60, sleep=slept.append)
    assert slept == [60]


def test_build_claude_cmd_system_prompt_only_when_set():
    base = build_claude_cmd("p", extra=["--x"], effort="high")
    assert "--system-prompt" not in base
    cmd = build_claude_cmd("p", extra=["--x"], effort="high", system_prompt="SYS")
    i = cmd.index("--x")
    assert cmd[i + 1:i + 3] == ["--system-prompt", "SYS"]
    assert cmd[i + 3] == "--effort"


def _official_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setattr(config, "PROTOCOL", "official")
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    calls = []

    def fake_build(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return ["claude"]
    with patch.object(agent_computer, "build_claude_cmd", fake_build), \
         patch.object(agent_computer, "_mcp_config", lambda url: str(tmp_path / "m.json")):
        (tmp_path / "m.json").write_text("{}")
        agent_computer.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                           env=_FakeEnv(), out=tmp_path, dry=True)
    return calls[0]


def test_official_argv(monkeypatch, tmp_path):
    kw = _official_argv(monkeypatch, tmp_path)
    assert kw["prompt"] == "Do X"
    assert kw["system_prompt"].startswith("<SYSTEM_CAPABILITY>")
    assert kw["allowed_tools"] == ["mcp__osworld__computer"]
    assert kw["max_turns"] == 2 * config.MAX_STEPS + 20
    disallowed = kw["extra"][kw["extra"].index("--disallowedTools") + 1:]
    assert "Read" in disallowed and "Bash" in disallowed


def test_official_argv_ignores_legacy_restriction_arms(monkeypatch, tmp_path):
    """The G5 deny-list arms must not add a second --disallowedTools/--strict-mcp-config."""
    monkeypatch.setattr(config, "ENFORCE_SANDBOX", True)
    monkeypatch.setattr(config, "RESTRICT_RUN_PYTHON", True)
    extra = _official_argv(monkeypatch, tmp_path)["extra"]
    assert extra.count("--disallowedTools") == 1
    assert extra.count("--strict-mcp-config") == 1
    assert extra == ["--disallowedTools", *agent_computer.CLAUDE_BUILTIN_TOOLS,
                     "--strict-mcp-config"]


def test_official_password_only_on_kvm(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KVM_CLIENT_PASSWORD", "sekret-pw")
    monkeypatch.setattr(config, "BACKEND", "daytona")
    assert "sekret-pw" not in _official_argv(monkeypatch, tmp_path)["system_prompt"]
    monkeypatch.setattr(config, "BACKEND", "kvm")
    assert "sekret-pw" in _official_argv(monkeypatch, tmp_path)["system_prompt"]


def test_builtin_tools_cover_every_non_mcp_tool():
    assert all(not t.startswith("mcp__") for t in agent_computer.CLAUDE_BUILTIN_TOOLS)
    for t in ("Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "Task", "Skill"):
        assert t in agent_computer.CLAUDE_BUILTIN_TOOLS


def _official_run(monkeypatch, tmp_path, transcript_lines):
    """Non-dry official run with the CLI, transcript copy and scoring faked out."""
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setattr(config, "PROTOCOL", "official")
    monkeypatch.setattr(config, "INLOOP_VERIFY", False)
    events = []
    scored = {}

    def fake_meta(cmd, **kw):
        events.append("claude")
        return {"result": "all good", "session_id": "s1", "subtype": "success",
                "is_error": False}

    def fake_save(meta, out, task_id=None):
        if transcript_lines is None:
            return {"transcript_saved": False, "transcript_error": "no file"}
        (out / "conversation.jsonl").write_text(
            "\n".join(json.dumps(l) for l in transcript_lines))
        return {"transcript_saved": True}

    def fake_score(ctrl, task, answer, out):
        events.append("score")
        scored["answer"] = answer
        return {"verdict": "FAILURE"}

    monkeypatch.setattr(agent_computer, "_mcp_config", lambda url: str(tmp_path / "m.json"))
    (tmp_path / "m.json").write_text("{}")
    monkeypatch.setattr(agent_computer, "run_claude_meta", fake_meta)
    monkeypatch.setattr(agent_computer, "_save_conversation_transcript", fake_save)
    monkeypatch.setattr(agent_computer, "_score", fake_score)
    monkeypatch.setattr(agent_computer, "protocol_wait", lambda s: events.append(("wait", s)))
    answer = agent_computer.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                                env=_FakeEnv(), out=tmp_path)
    return answer, events, scored


def test_official_run_answer_from_transcript_and_timings(monkeypatch, tmp_path):
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "cannot be done [INFEASIBLE]"}]}}]
    answer, events, scored = _official_run(monkeypatch, tmp_path, lines)
    assert answer == "FAIL" and scored["answer"] == "FAIL"
    assert events == [("wait", config.POST_SETUP_WAIT_S), "claude",
                      ("wait", config.PRE_EVAL_WAIT_S), "score"]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["answer"] == "FAIL"


def test_official_run_missing_transcript_falls_back_to_final_text(monkeypatch, tmp_path):
    # a leftover transcript from an earlier attempt must not be read as this run's
    (tmp_path / "conversation.jsonl").write_text(json.dumps(
        {"type": "assistant", "message": {"content": "[INFEASIBLE]"}}))
    answer, _, _ = _official_run(monkeypatch, tmp_path, None)
    assert answer == "DONE"
    # unverifiable, not clean: no transcript means no audit
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_non_computer_tool_calls"] is None


# --- pre-review fixes: tool drift guards, in-loop verify refusal, clean finish, provenance ---

import pytest  # noqa: E402


def _stream(tools):
    return "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "hook_started", "hook_id": "h"},
        {"type": "system", "subtype": "init", "tools": tools},
        {"type": "result", "subtype": "success"},
    ]) + "\n"


def test_tool_preflight_passes_when_every_builtin_is_known():
    tools = [*agent_computer.CLAUDE_BUILTIN_TOOLS, "mcp__playwright__browser_click"]
    assert agent_computer.official_tool_preflight(stream=_stream(tools)) is None


def test_tool_preflight_finds_init_after_hook_events_and_names_unknown_tools():
    tools = [*agent_computer.CLAUDE_BUILTIN_TOOLS, "Glob", "mcp__x__y", "NewTool"]
    with pytest.raises(SystemExit) as e:
        agent_computer.official_tool_preflight(stream=_stream(tools))
    assert "Glob" in str(e.value) and "NewTool" in str(e.value)
    assert "mcp__x__y" not in str(e.value)


def test_tool_preflight_refuses_without_an_init_event():
    with pytest.raises(SystemExit):
        agent_computer.official_tool_preflight(stream='{"type": "system", "subtype": "x"}\nnoise')


def test_tool_preflight_runs_the_cli_with_the_configured_model(monkeypatch):
    seen = {}

    def fake_raw(cmd, *, timeout, env=None):
        seen["cmd"] = cmd
        return _stream(agent_computer.CLAUDE_BUILTIN_TOOLS)
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    monkeypatch.setattr(agent_computer, "_run_raw", fake_raw)
    agent_computer.official_tool_preflight()
    cmd = seen["cmd"]
    assert cmd[:3] == ["claude", "-p", "reply ok"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd and cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"


def test_runner_preflight_only_adds_protocol_checks_under_official(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_computer.osworld_eval, "pinned_code_preflight",
                        lambda: calls.append("pinned"))
    monkeypatch.setattr(agent_computer, "official_tool_preflight",
                        lambda: calls.append("tools"))
    monkeypatch.setattr(config, "INLOOP_VERIFY", False)
    monkeypatch.setattr(config, "OFFICIAL", False)
    agent_computer.preflight()
    assert calls == ["pinned"]
    monkeypatch.setattr(config, "OFFICIAL", True)
    agent_computer.preflight()
    assert calls == ["pinned", "pinned", "tools"]


def test_official_refuses_inloop_verify_at_preflight_and_run(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_computer.osworld_eval, "pinned_code_preflight", lambda: None)
    monkeypatch.setattr(agent_computer, "official_tool_preflight", lambda: None)
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setattr(config, "INLOOP_VERIFY", True)
    with pytest.raises(SystemExit, match="INLOOP_VERIFY"):
        agent_computer.preflight()
    with pytest.raises(SystemExit, match="INLOOP_VERIFY"):
        agent_computer.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                           env=_FakeEnv(), out=tmp_path, dry=True)


def test_transcript_tool_names(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__osworld__computer", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "assistant", "message": {"content": "text only"}},
    ]))
    assert common.claude_transcript_tool_names(p) == ["mcp__osworld__computer", "Bash"]


def test_official_run_audits_non_computer_tool_calls(monkeypatch, tmp_path):
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__osworld__computer", "input": {"action": "x"}},
        {"type": "tool_use", "name": "Read", "input": {}}]}}]
    _official_run(monkeypatch, tmp_path, lines)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_non_computer_tool_calls"] == ["Read"]


def test_official_run_clean_audit_is_empty(monkeypatch, tmp_path):
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__osworld__computer", "input": {"action": "x"}}]}}]
    _official_run(monkeypatch, tmp_path, lines)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_non_computer_tool_calls"] == []


@pytest.mark.parametrize("meta,expected", [
    ({"subtype": "success", "is_error": False, "result": "no answer line"}, True),
    ({"subtype": "error_max_turns", "is_error": False, "result": "ANSWER: DONE"}, False),
    ({"subtype": "success", "is_error": True, "result": "ANSWER: DONE"}, False),
    ({}, False),   # timeout/unparseable envelope
])
def test_official_clean_finish_from_cli_metadata(meta, expected):
    assert agent_computer._official_clean_finish(meta) is expected


def test_official_run_records_clean_finish_without_answer_line(monkeypatch, tmp_path):
    _official_run(monkeypatch, tmp_path, [])
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_clean_finish"] is True


def test_legacy_clean_finish_unchanged():
    assert common._clean_finish({"subtype": "success"}, "") is False
    assert common._clean_finish({"is_error": False}, "DONE") is True


def test_official_provenance_records_the_cli_turn_limit(monkeypatch, tmp_path):
    _official_run(monkeypatch, tmp_path, [])
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["provenance"]["max_turns"] == 2 * config.MAX_STEPS + 20


def test_legacy_provenance_turn_limit_unchanged(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL", False)
    monkeypatch.setattr(config, "MAX_TURNS", 150)
    prov = agent_computer._run_provenance({"id": "t"}, None, "now")
    assert prov["max_turns"] == 150
