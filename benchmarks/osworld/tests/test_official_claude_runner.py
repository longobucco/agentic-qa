import json
import threading
import time
from unittest.mock import patch

import pytest

from benchmarks.osworld import config
from benchmarks.osworld.runners import agent_computer, common
from core import procgroups
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


def test_protocol_wait_calls_sleep(monkeypatch):
    slept = []
    common.protocol_wait(60, sleep=slept.append)
    assert slept == [60]


def test_protocol_wait_is_interrupted_by_the_flag_instead_of_blocking_the_full_wait(monkeypatch):
    """task-10b fix round 2: without this, an in-flight worker inside the real
    POST_SETUP_WAIT_S=60 settle sleep only notices a harness SIGTERM/SIGINT at the NEXT
    spawner call -- long enough that core.run's shutdown(wait=True) could still be blocked
    when the campaign driver's own grace period SIGKILLs the whole process, losing the
    INTERRUPTED infra record entirely. protocol_wait must return (by raising) the moment the
    flag is set, not after `seconds`."""
    timer = threading.Timer(0.05, procgroups.mark_interrupted)
    timer.start()
    try:
        t0 = time.monotonic()
        with pytest.raises(procgroups.Interrupted):
            common.protocol_wait(30)   # no `sleep=` override: exercises the real Event wait
        elapsed = time.monotonic() - t0
        assert elapsed < 5, (
            f"protocol_wait blocked {elapsed:.1f}s instead of returning promptly on interrupt")
    finally:
        timer.cancel()
        procgroups._reset_for_tests()


def test_build_claude_cmd_system_prompt_only_when_set():
    base = build_claude_cmd("p", extra=["--x"], effort="high")
    assert "--system-prompt" not in base
    cmd = build_claude_cmd("p", extra=["--x"], effort="high", system_prompt="SYS")
    i = cmd.index("--x")
    assert cmd[i + 1:i + 3] == ["--system-prompt", "SYS"]
    assert cmd[i + 3] == "--effort"


def _official_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROTOCOL", "official")
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    calls = []

    def fake_build(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return ["claude"]
    with patch.object(agent_computer, "build_claude_cmd", fake_build), \
         patch.object(agent_computer, "_mcp_config", lambda url, **kw: str(tmp_path / "m.json")):
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


def _official_run(monkeypatch, tmp_path, transcript_lines, spy=None):
    """Non-dry official run with the CLI, transcript copy and scoring faked out."""
    monkeypatch.setattr(config, "PROTOCOL", "official")
    events = []
    scored = {}

    def fake_meta(cmd, **kw):
        events.append("claude")
        if spy:
            spy(cmd, **kw)
        # the official MCP server's liveness/step state (final fix wave S2)
        (tmp_path / "mcp_state.json").write_text('{"started": true, "steps_used": 2}')
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

    monkeypatch.setattr(agent_computer, "_mcp_config", lambda url, **kw: str(tmp_path / "m.json"))
    (tmp_path / "m.json").write_text("{}")
    monkeypatch.setattr(agent_computer, "run_claude_meta", fake_meta)
    monkeypatch.setattr(agent_computer, "claude_cli_version", lambda **kw: "2.1.280")
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


def test_run_claude_meta_interrupted_propagates_without_writing_eval(monkeypatch, tmp_path):
    """task-10b fix round 1: core.procgroups.Interrupted (the harness itself was SIGTERM'd/
    SIGINT'd mid-agent-call, not the agent's own timeout) must reach core.run's work() intact --
    agent_computer.run()'s only wrapping around the main run_claude_meta call is a bare
    try/finally (mcp_config cleanup), never an `except Exception`, so this is a regression guard
    that no future refactor adds one that would swallow it into a scored/failed result."""
    monkeypatch.setattr(config, "PROTOCOL", "official")

    def fake_meta(cmd, **kw):
        raise procgroups.Interrupted("harness interrupted")

    monkeypatch.setattr(agent_computer, "_mcp_config", lambda url, **kw: str(tmp_path / "m.json"))
    (tmp_path / "m.json").write_text("{}")
    monkeypatch.setattr(agent_computer, "run_claude_meta", fake_meta)
    monkeypatch.setattr(agent_computer, "protocol_wait", lambda s: None)

    with pytest.raises(procgroups.Interrupted):
        agent_computer.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                           env=_FakeEnv(), out=tmp_path)
    assert not (tmp_path / "eval.json").exists()
    assert not (tmp_path / "result.json").exists()


def test_official_run_missing_transcript_falls_back_to_final_text(monkeypatch, tmp_path):
    # a leftover transcript from an earlier attempt must not be read as this run's
    (tmp_path / "conversation.jsonl").write_text(json.dumps(
        {"type": "assistant", "message": {"content": "[INFEASIBLE]"}}))
    answer, events, _ = _official_run(monkeypatch, tmp_path, None)
    # unverifiable, not clean: no transcript means no audit
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["answer"] == "DONE"
    assert result["agent_non_computer_tool_calls"] is None
    # final fix wave S1: an unaudited run is never scored (parity with the Codex arm)
    assert answer == "" and "score" not in events
    infra = json.loads((tmp_path / "infra_error.json").read_text())[-1]
    assert infra["error_type"] == "ToolAuditUnavailable"
    assert not (tmp_path / "eval.json").exists()


# --- pre-review fixes: tool drift guards, in-loop verify refusal, clean finish, provenance ---

import pytest  # noqa: E402


def test_tool_preflight_passes_when_every_builtin_is_known(monkeypatch, tmp_path):
    # any other MCP tool is refused (final fix wave S3, test_final_fix_shared.py)
    tools = [*agent_computer.CLAUDE_BUILTIN_TOOLS, "mcp__osworld__computer"]
    _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x") + [_snapshot([])], tools=tools)
    assert agent_computer.official_tool_preflight() is None


def test_tool_preflight_finds_init_after_hook_events_and_names_unknown_tools(monkeypatch, tmp_path):
    tools = [*agent_computer.CLAUDE_BUILTIN_TOOLS, "OtherTool", "mcp__x__y", "NewTool"]
    _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x"), tools=tools)
    with pytest.raises(SystemExit) as e:
        agent_computer.official_tool_preflight()
    assert "OtherTool" in str(e.value) and "NewTool" in str(e.value)
    assert "mcp__x__y" not in str(e.value)


def test_tool_preflight_refuses_without_an_init_event(monkeypatch):
    monkeypatch.setattr(agent_computer, "_run_raw",
                        lambda cmd, **kw: '{"type": "system", "subtype": "x"}\nnoise')
    with pytest.raises(SystemExit):
        agent_computer.official_tool_preflight()


def test_tool_preflight_runs_the_cli_with_the_configured_model(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    seen = _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x") + [_snapshot([])])
    agent_computer.official_tool_preflight()
    cmd = seen["cmd"]
    assert cmd[:3] == ["claude", "-p", "reply ok"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd and cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"


def test_preflight_always_runs_pinned_code_version_and_tool_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    monkeypatch.setattr(agent_computer.osworld_eval, "pinned_code_preflight",
                        lambda: calls.append("pinned"))
    monkeypatch.setattr(agent_computer, "official_tool_preflight",
                        lambda: calls.append("tools"))
    monkeypatch.setattr(agent_computer, "claude_version_preflight",
                        lambda: calls.append("version"))
    agent_computer.preflight()
    assert calls == ["pinned", "version", "tools"]


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


def test_official_run_terminal_failure_on_tool_surface_violation(monkeypatch, tmp_path):
    # Task 7b, parity with the Codex arm: a non-empty agent_non_computer_tool_calls is a
    # terminal FAILURE (never infra_error.json), so resume never selectively re-runs it.
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__osworld__computer", "input": {"action": "x"}},
        {"type": "tool_use", "name": "Read", "input": {}}]}}]
    answer, events, scored = _official_run(monkeypatch, tmp_path, lines)
    verdict = json.loads((tmp_path / "eval.json").read_text())
    assert verdict == {
        "id": "t", "verdict": "FAILURE", "reward": 0.0, "source": "harness",
        "reason": "tool surface violation: Read",
        "tool_surface_violation": ["Read"],
    }
    assert not (tmp_path / "infra_error.json").exists()
    assert "score" not in events
    assert ("wait", config.PRE_EVAL_WAIT_S) not in events
    from core import results as results_io
    assert results_io.is_done(tmp_path)


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


def test_official_provenance_records_the_cli_turn_limit(monkeypatch, tmp_path):
    _official_run(monkeypatch, tmp_path, [])
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["provenance"]["max_turns"] == 2 * config.MAX_STEPS + 20


# --- fix round 1: host-context isolation and the leak guard ---

def _leaky_lines(repo):
    return [
        {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                              "content": ["You have superpowers."]}},
        {"type": "attachment", "attachment": {"type": "instructions", "files": [
            {"path": "/h/.claude/projects/x/memory/MEMORY.md", "type": "AutoMem"}]}},
        {"type": "attachment", "attachment": {"type": "session_context", "context": {
            "userEmail": "The user's email address is a@b.c.", "gitStatus": "M x"}}},
        {"type": "attachment", "attachment": {"type": "environment", "snapshot": {
            "workingDirectory": repo, "isGitRepo": True}}},
    ]


def _clean_lines(tmpdir):
    return [
        {"type": "attachment", "attachment": {"type": "environment", "snapshot": {
            "workingDirectory": tmpdir, "isGitRepo": False}}},
        {"type": "attachment", "attachment": {"type": "date", "date": "2026-09-24"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    ]


def _write(p, lines):
    p.write_text("\n".join(json.dumps(l) for l in lines))
    return p


def test_leak_scanner_flags_every_host_context_kind(tmp_path):
    repo = str(common.CHECKOUT_ROOT)
    leaks = common.claude_transcript_context_leaks(_write(tmp_path / "c.jsonl", _leaky_lines(repo)))
    assert leaks == ["git_status", "hook_context", "memory", "repo_path", "user_email"]


def test_leak_scanner_clean_transcript(tmp_path):
    # the default system prompt's MEMORY.md instructions in a snapshot are not a memory leak
    lines = _clean_lines("/private/tmp/osw_claude_x") + [
        {"type": "attachment", "attachment": {"type": "prompt_snapshot",
                                              "systemPrompt": ["add a pointer in `MEMORY.md`"]}}]
    p = _write(tmp_path / "c.jsonl", lines)
    assert common.claude_transcript_context_leaks(p) == []


def test_official_kwargs_carry_isolation_settings(monkeypatch, tmp_path):
    extra = _official_argv(monkeypatch, tmp_path)["extra"]
    i = extra.index("--setting-sources")
    assert extra[i + 1] == ""
    settings = json.loads(extra[extra.index("--settings") + 1])
    assert settings == {"disableAllHooks": True}
    assert extra.count("--disallowedTools") == 1


def test_official_mcp_config_carries_pythonpath(monkeypatch):
    path = common._mcp_config("http://x")
    try:
        spec = json.loads(open(path).read())["mcpServers"]["osworld"]
    finally:
        import os
        os.unlink(path)
    assert spec["env"]["PYTHONPATH"] == str(common.CHECKOUT_ROOT)


def test_official_run_uses_a_fresh_empty_cwd_and_records_leaks(monkeypatch, tmp_path):
    import os
    seen = {}

    def spy(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        seen["empty"] = bool(kw.get("cwd")) and os.path.isdir(kw["cwd"]) and not os.listdir(kw["cwd"])
    _official_run(monkeypatch, tmp_path, _leaky_lines(str(common.CHECKOUT_ROOT)), spy=spy)
    assert seen["empty"] is True
    assert not str(seen["cwd"]).startswith(str(common.CHECKOUT_ROOT))
    assert not os.path.exists(seen["cwd"])   # removed after the run
    result = json.loads((tmp_path / "result.json").read_text())
    assert "hook_context" in result["agent_context_leaks"]


def test_official_run_leaks_none_without_transcript(monkeypatch, tmp_path):
    _official_run(monkeypatch, tmp_path, None)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_context_leaks"] is None


def _probe(monkeypatch, tmp_path, transcript_lines, tools=None):
    """official_tool_preflight against a fake CLI: returns the argv/cwd it ran with."""
    seen = {}
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    monkeypatch.setattr(common.Path, "home", classmethod(lambda cls: home))

    def fake_raw(cmd, *, timeout, env=None, cwd=None):
        seen.update(cmd=cmd, cwd=cwd)
        if transcript_lines is not None:
            _write(proj / "sid-1.jsonl", transcript_lines)
        stream = [{"type": "system", "subtype": "hook_started"},
                  {"type": "system", "subtype": "init", "session_id": "sid-1",
                   "tools": tools if tools is not None else agent_computer.CLAUDE_BUILTIN_TOOLS}]
        return "\n".join(json.dumps(e) for e in stream)
    monkeypatch.setattr(agent_computer, "_run_raw", fake_raw)
    return seen


def test_tool_preflight_probe_uses_the_run_isolation(monkeypatch, tmp_path):
    seen = _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x") + [_snapshot([])])
    agent_computer.official_tool_preflight()
    cmd = seen["cmd"]
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--settings" in cmd and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--system-prompt") + 1].startswith("<SYSTEM_CAPABILITY>")
    assert seen["cwd"] and not str(seen["cwd"]).startswith(str(common.CHECKOUT_ROOT))


def test_tool_preflight_fails_closed_on_context_leaks(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _leaky_lines(str(common.CHECKOUT_ROOT)) + [_snapshot([])])
    with pytest.raises(SystemExit) as e:
        agent_computer.official_tool_preflight()
    for kind in ("hook_context", "memory", "git_status", "repo_path"):
        assert kind in str(e.value)


def test_tool_preflight_tolerates_only_the_oauth_account_email(monkeypatch, tmp_path):
    lines = _clean_lines("/private/tmp/x") + [
        {"type": "attachment", "attachment": {"type": "session_context", "context": {
            "userEmail": "The user's email address is a@b.c."}}}, _snapshot([])]
    _probe(monkeypatch, tmp_path, lines)
    agent_computer.official_tool_preflight()   # does not raise


def test_tool_preflight_fails_closed_without_the_probe_transcript(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, None)
    with pytest.raises(SystemExit, match="transcript"):
        agent_computer.official_tool_preflight()


def _snapshot(tools):
    return {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x"],
                                                 "tools": [{"name": t} for t in tools]}}


def test_offered_tools_come_from_the_last_prompt_snapshot(tmp_path):
    p = _write(tmp_path / "c.jsonl", [
        {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x"]}},
        _snapshot(["Glob", "mcp__osworld__computer"])])
    assert common.claude_transcript_offered_tools(p) == ["Glob", "mcp__osworld__computer"]
    assert common.claude_transcript_offered_tools(_write(tmp_path / "d.jsonl", [])) is None


def test_hidden_builtins_seen_only_in_the_snapshot_are_denied():
    for t in ("Glob", "Grep", "ListMcpResourcesTool", "ReadMcpResourceTool",
              "ReadMcpResourceDirTool"):
        assert t in agent_computer.CLAUDE_BUILTIN_TOOLS


def test_tool_preflight_probe_runs_with_the_full_official_deny_list(monkeypatch, tmp_path):
    seen = _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x") + [_snapshot([])])
    agent_computer.official_tool_preflight()
    cmd = seen["cmd"]
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]
    assert denied == agent_computer.CLAUDE_BUILTIN_TOOLS


def test_tool_preflight_fails_closed_when_a_builtin_is_still_offered(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x") + [_snapshot(["LSP"])])
    with pytest.raises(SystemExit, match="LSP"):
        agent_computer.official_tool_preflight()


def test_tool_preflight_fails_closed_without_a_tool_snapshot(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _clean_lines("/private/tmp/x"))
    with pytest.raises(SystemExit, match="offered"):
        agent_computer.official_tool_preflight()


def test_official_run_records_offered_tools(monkeypatch, tmp_path):
    _official_run(monkeypatch, tmp_path, [_snapshot(["mcp__osworld__computer"])])
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["agent_offered_tools"] == ["mcp__osworld__computer"]
