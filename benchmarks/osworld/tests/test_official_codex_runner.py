"""The Codex (GPT-Astra) runner under the official protocol -- mirrors
test_official_claude_runner.py so both arms get the same protocol (no model call)."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.osworld import config, official_protocol
from benchmarks.osworld.runners import astra_common, common, gpt_astra, gpt_astra_openbook
from core import codex_loop
from core.codex_loop import build_codex_cmd

ROOT = Path(__file__).resolve().parents[3]
OFFICIAL_LOCK = ROOT / "benchmarks/osworld/astra_official361_lock.json"


def _ev(item, phase="item.completed"):
    return {"type": phase, "item": item}


def _computer(i, args, phase="item.completed", tool="computer"):
    return _ev({"id": f"item_{i}", "type": "mcp_tool_call", "server": "osworld", "tool": tool,
                "arguments": args, "status": "completed"}, phase)


def _msg(i, text):
    return _ev({"id": f"item_{i}", "type": "agent_message", "text": text})


# --- answer extraction over the Codex event log ---

def test_events_actions_extract_messages_and_computer_arguments():
    events = [
        {"type": "thread.started", "thread_id": "x"},
        _msg(0, "looking"),
        _computer(1, {"action": "screenshot"}, phase="item.started"),
        _computer(1, {"action": "screenshot"}),
        _computer(2, json.dumps({"action": "left_click", "coordinate": [1, 2]})),
        _computer(3, {"x": 1}, tool="other"),
        _msg(4, "[INFEASIBLE]"),
    ]
    texts, calls = gpt_astra.codex_events_actions(events)
    assert texts == ["looking", "[INFEASIBLE]"]
    assert calls == [{"action": "screenshot"}, {"action": "left_click", "coordinate": [1, 2]}]


def test_events_actions_keep_a_call_cut_off_before_completion():
    texts, calls = gpt_astra.codex_events_actions(
        [_computer(1, '{"action": "fail"}', phase="item.started"), "garbage", {"item": 3}])
    assert texts == [] and calls == [{"action": "fail"}]


def test_final_action_over_events_with_infeasible_is_fail():
    events = [_msg(0, "This app cannot do it. [INFEASIBLE]"), _computer(1, {"action": "key"})]
    assert official_protocol.final_action(*gpt_astra.codex_events_actions(events)) == "FAIL"
    assert official_protocol.final_action(
        *gpt_astra.codex_events_actions([_msg(0, "done")])) == "DONE"


# --- command construction ---

def test_base_instructions_go_through_the_model_instructions_file(tmp_path):
    cmd = build_codex_cmd("do it", model="m", cwd=tmp_path, controller_url="",
                          base_instructions="SYS PROMPT", extra_config=["a=1", "b.c=false"])
    path = codex_loop.base_instructions_file(cmd)
    try:
        assert Path(path).read_text() == "SYS PROMPT"
        assert f"model_instructions_file={json.dumps(path)}" in cmd
        assert cmd[-5:] == ["-c", "a=1", "-c", "b.c=false", "do it"]
    finally:
        os.unlink(path)


def test_without_base_instructions_the_command_is_unchanged(tmp_path):
    cmd = build_codex_cmd("do it", model="m", cwd=tmp_path, controller_url="")
    assert codex_loop.base_instructions_file(cmd) is None
    assert not any(a.startswith("model_instructions_file") for a in cmd)
    assert cmd[-3:] == ["-c", cmd[-2], "do it"] and cmd[-2].startswith("mcp_servers.osworld.env=")


class _FakeEnv:
    browser = None
    setup_error = None


def _official(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setattr(config, "PROTOCOL", "official")


def test_official_dry_run_command(monkeypatch, tmp_path, capsys):
    _official(monkeypatch)
    calls = []
    real = gpt_astra.build_codex_cmd

    def spy(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return real(prompt, **kw)
    monkeypatch.setattr(gpt_astra, "build_codex_cmd", spy)
    gpt_astra.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                  env=_FakeEnv(), out=tmp_path, dry=True)
    kw = calls[0]
    assert kw["prompt"] == "Do X"
    assert kw["base_instructions"] == gpt_astra._official_system_prompt()
    assert "<SYSTEM_CAPABILITY>" in kw["base_instructions"]
    assert kw["mcp_extra_env"]["OSW_PROTOCOL"] == "official"
    assert tuple(kw["extra_config"]) == gpt_astra.CODEX_ISOLATION_CONFIG
    printed = capsys.readouterr().out
    assert "model_instructions_file=" in printed and "OSW_PROTOCOL" in printed
    assert "Do X" in printed
    # the instructions temp file does not outlive the dry run
    path = printed.split("model_instructions_file=")[1].split('"')[1]
    assert not Path(path).exists()


def test_legacy_dry_run_command_has_no_protocol_parts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFICIAL", False)
    calls = []
    monkeypatch.setattr(gpt_astra, "build_codex_cmd",
                        lambda prompt, **kw: calls.append((prompt, kw)) or ["codex"])
    gpt_astra.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                  env=_FakeEnv(), out=tmp_path, dry=True)
    prompt, kw = calls[0]
    assert prompt != "Do X" and "Do X" in prompt
    assert "base_instructions" not in kw and "extra_config" not in kw


# --- campaign lock ---

def test_official_lock_validates_once_effort_matches(monkeypatch):
    _official(monkeypatch)
    monkeypatch.setattr(gpt_astra, "_LOCK", OFFICIAL_LOCK)
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "REQUIRES_USER_DECISION")
    monkeypatch.setattr(config, "ZOOM_BATCH", False)
    gpt_astra._validate_campaign_lock()


def test_official_lock_contents():
    lock = json.loads(OFFICIAL_LOCK.read_text())
    base = json.loads((ROOT / "benchmarks/osworld/astra_protocol361_lock.json").read_text())
    assert lock["tool_policy"]["allowed_mcp_tools"] == ["computer"]
    assert lock["reasoning_effort"] == "REQUIRES_USER_DECISION"
    assert lock["population"] == base["population"]
    assert lock["protocol"] == {
        "plan": "docs/superpowers/plans/2026-09-24-osworld-official-fidelity.md",
        "protocol": "official", "backend": "kvm", "screen": "1920x1080", "max_steps": 100,
        "system_prompt_channel": "model_instructions_file"}
    assert tuple(lock["tool_policy"]["isolation_config"]) == gpt_astra.CODEX_ISOLATION_CONFIG


def test_official_lock_refuses_undecided_effort_and_the_legacy_protocol(monkeypatch):
    monkeypatch.setattr(gpt_astra, "_LOCK", OFFICIAL_LOCK)
    _official(monkeypatch)
    with pytest.raises(SystemExit, match="reasoning_effort"):
        gpt_astra._validate_campaign_lock()   # effort not decided yet
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "REQUIRES_USER_DECISION")
    monkeypatch.setattr(config, "OFFICIAL", False)
    with pytest.raises(SystemExit, match="tool policy"):
        gpt_astra._validate_campaign_lock()   # the computer-only lock is not the legacy arm


def test_legacy_lock_refused_under_official(monkeypatch):
    _official(monkeypatch)
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld/astra_protocol361_lock.json")
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "REQUIRES_USER_DECISION")
    with pytest.raises(SystemExit, match="tool policy"):
        gpt_astra._validate_campaign_lock()


# --- the official run ---

def _official_run(monkeypatch, tmp_path, events, *, raw=None, errors=None, rollout="clean"):
    _official(monkeypatch)
    if rollout == "clean":
        rollout = tmp_path / "clean_rollout.jsonl"
        rollout.write_text(_rollout_lines(leaky=False))
    order, scored = [], {}
    raw = "\n".join(json.dumps(e) for e in events) + "\n" if raw is None else raw
    meta = {"result": "", "session_id": "s1", "usage": {}, "errors": errors,
            "is_error": bool(errors), "timed_out": False, "returncode": 0, "num_turns": 1,
            "tool_calls": 0, "mcp_tool_calls": 0, "non_mcp_tool_calls": 0, "tool_names": [],
            "events": events, "raw": raw, "stderr": ""}

    def fake_meta(cmd, **kw):
        order.append("codex")
        return dict(meta)

    def fake_score(ctrl, task, answer, out):
        order.append("score")
        scored["answer"] = answer
        return {"verdict": "FAILURE", "reward": 0.0}
    monkeypatch.setattr(gpt_astra, "run_codex_meta", fake_meta)
    monkeypatch.setattr(gpt_astra, "_codex_version", lambda: "codex-cli 0.153.4")
    monkeypatch.setattr(gpt_astra, "_score", fake_score)
    monkeypatch.setattr(gpt_astra, "protocol_wait", lambda s: order.append(("wait", s)))
    monkeypatch.setattr(gpt_astra, "_rollout_path", lambda sid: rollout)
    answer = gpt_astra.run({"id": "t", "instruction": "Do X", "evaluator": {}},
                           env=_FakeEnv(), out=tmp_path)
    read = lambda n: json.loads((tmp_path / n).read_text()) if (tmp_path / n).exists() else None
    return answer, order, scored, read("result.json"), read("eval.json"), read("infra_error.json")


def test_official_run_answer_from_events_and_timings(monkeypatch, tmp_path):
    answer, order, scored, result, verdict, infra = _official_run(
        monkeypatch, tmp_path, [_msg(0, "cannot be done [INFEASIBLE]")])
    assert answer == "FAIL" and scored["answer"] == "FAIL" and result["answer"] == "FAIL"
    assert order == [("wait", config.POST_SETUP_WAIT_S), "codex",
                     ("wait", config.PRE_EVAL_WAIT_S), "score"]
    assert verdict and infra is None
    assert result["agent_clean_finish"] is True


def test_official_run_done_without_an_answer_line(monkeypatch, tmp_path):
    answer, *_ = _official_run(monkeypatch, tmp_path,
                               [_computer(1, {"action": "left_click"}), _msg(2, "All set.")])
    assert answer == "DONE"


def test_official_run_missing_transcript_falls_back_to_final_text(monkeypatch, tmp_path):
    _, order, _, result, verdict, infra = _official_run(monkeypatch, tmp_path, [], raw="")
    assert result["answer"] == "DONE"
    # no event log: tool use is unverifiable, so the run is not scored
    assert result["agent_non_computer_tool_calls"] is None
    assert verdict is None and infra[-1]["error_type"] == "ToolAuditUnavailable"
    assert "score" not in order


def test_official_rate_limited_run_skips_the_pre_eval_wait(monkeypatch, tmp_path):
    _, order, _, result, verdict, infra = _official_run(
        monkeypatch, tmp_path, [_msg(0, "hi")], errors=[{"message": "429 usage limit"}])
    assert order == [("wait", config.POST_SETUP_WAIT_S), "codex"]
    assert verdict is None and infra[-1]["outcome"] == "RATE_LIMITED"


def test_official_run_refuses_to_score_a_non_computer_tool_call(monkeypatch, tmp_path):
    events = [_computer(1, {"action": "screenshot"}),
              _ev({"id": "item_2", "type": "web_search", "query": "answer"})]
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_rollout_lines(leaky=False, extra=[
        _exec("await tools.mcp__osworld__computer({action: 'screenshot'})"),
        _exec("await tools.apply_patch('*** Begin Patch')")]))
    _, order, _, result, verdict, infra = _official_run(monkeypatch, tmp_path, events,
                                                        rollout=rollout)
    assert result["agent_non_computer_tool_calls"] == ["web_search", "exec.apply_patch"]
    assert verdict is None and infra[-1]["error_type"] == "ToolSurfaceViolation"
    assert "score" not in order


def test_official_run_records_the_audit(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_rollout_lines(leaky=False))
    _, _, _, result, _, _ = _official_run(
        monkeypatch, tmp_path, [_computer(1, {"action": "screenshot"})], rollout=rollout)
    assert result["agent_non_computer_tool_calls"] == []
    assert result["agent_context_leaks"] == []
    assert result["agent_offered_tools"] is None   # not recorded per run by Codex (see runner)


def test_official_run_without_a_rollout_is_not_scored(monkeypatch, tmp_path):
    _, order, _, result, verdict, infra = _official_run(
        monkeypatch, tmp_path, [_computer(1, {"action": "screenshot"}), _msg(2, "x")],
        rollout=None)
    assert result["agent_context_leaks"] is None
    # exec calls are only in the rollout: without it tool use is unverifiable, not clean
    assert result["agent_non_computer_tool_calls"] is None
    assert verdict is None
    assert infra[-1]["outcome"] == "HARNESS_ERROR"
    assert infra[-1]["error_type"] == "ToolAuditUnavailable"
    assert order == [("wait", config.POST_SETUP_WAIT_S), "codex"]


def _exec(code):
    return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                                                 "input": code, "call_id": "c"}}


def _function_call(name):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name,
                                                 "arguments": "{}", "call_id": "f"}}


def test_rollout_tool_calls_see_inside_the_code_mode_host(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(_rollout_lines(leaky=False, extra=[
        _exec("const r = await tools.mcp__osworld__computer({action: 'screenshot'});\n"
              "image(r.content[0]); text(JSON.stringify(ALL_TOOLS.map(t => t.name)))"),
        _function_call("wait"),
        _exec("await tools['clock__curr_time']({}); await tools . read_mcp_resource({})"),
        _exec("const f = ALL_TOOLS[0].name; await tools[f]({})"),
        _function_call("spawn_agent"),
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "other"}},
        # the computer call in every accepted spelling is never flagged
        _exec("await tools?.mcp__osworld__computer({}); await tools[\"mcp__osworld__computer\"]"
              "({}); await tools?.['mcp__osworld__computer']({})"),
    ]))
    assert astra_common.codex_rollout_tool_calls(p) == [
        "exec.clock__curr_time", "exec.read_mcp_resource", "exec.<dynamic>", "spawn_agent",
        "other"]


@pytest.mark.parametrize("code", [
    'const { apply_patch } = tools; await apply_patch("x")',
    "await globalThis.tools.apply_patch(1)",
    "await tools?.apply_patch(1)",
    "const t = tools; t.clock__curr_time()",
    "const { mcp__osworld__computer: c, ...rest } = tools; await rest.apply_patch(1)",
])
def test_rollout_tool_calls_fail_closed_on_any_other_use_of_tools(tmp_path, code):
    p = tmp_path / "r.jsonl"
    p.write_text(_rollout_lines(leaky=False, extra=[_exec(code)]))
    assert astra_common.codex_rollout_tool_calls(p) != []


# --- host-context leaks, from Codex's own rollout ---

def _message(role, kind, text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"content_item_kinds": [kind]}}}


def _rollout_lines(*, leaky, provenance="custom", extra=()):
    recs = [{"type": "session_meta", "payload": {
        "cwd": "/tmp/osw-astra-x", "base_instructions": {
            "text": "SYS", "provenance": {"type": provenance}}}}]
    if leaky:
        recs += [_message("developer", "host_skills.instructions", "<skills_instructions>"),
                 _message("user", "environments.environment_context", "<environment_context>")]
    recs += [_message("developer", "multi_agent.role_instructions", "<multi_agent_role>"),
             _message("developer", "multi_agent.mode_instructions", "<multi_agent_mode>"),
             _message("user", "user.text", "Do X"), *extra,
             {"type": "response_item", "payload": {
                 "type": "message", "role": "assistant", "content": [{"text": "ok"}],
                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["unknown"]}}}]
    return "\n".join(json.dumps(r) for r in recs) + "\nnot json\n"


def test_rollout_leak_scanner(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(_rollout_lines(leaky=False))
    assert astra_common.codex_rollout_context_leaks(p) == []
    p.write_text(_rollout_lines(leaky=True, provenance="model"))
    assert astra_common.codex_rollout_context_leaks(p) == [
        "base_instructions:model", "environments.environment_context",
        "host_skills.instructions"]
    p.write_text(_rollout_lines(leaky=False, extra=[
        _message("user", "user.text", f"see {common.CHECKOUT_ROOT}/README")]))
    assert astra_common.codex_rollout_context_leaks(p) == ["repo_path"]


# --- offered tools and the session preflight ---

def _trace(names_by_ns, nested=()):
    tools = [{"type": "namespace", "name": ns, "tools": [
        {"type": "function", "name": n,
         "description": "".join(f"declare const tools: {{ {x}(args: {{}}): Promise<unknown>; }};"
                                for x in nested) if n == "exec" else ""}
        for n in names]} for ns, names in names_by_ns.items()]
    line = json.dumps({"type": "response.created", "response": {"tools": tools}},
                      separators=(",", ":"))
    return f"noise\n2026 TRACE tungstenite::protocol: Received message {line}\n"


def test_offered_tools_from_trace():
    trace = _trace({"functions": ["wait", "exec"], "collaboration": ["spawn_agent"]},
                   nested=["apply_patch", "web__run"])
    assert codex_loop.offered_tools_from_trace(trace) == [
        "functions.wait", "functions.exec", "exec.apply_patch", "exec.web__run",
        "collaboration.spawn_agent"]
    assert codex_loop.offered_tools_from_trace("no events") is None


def _tolerated_trace():
    by_ns, nested = {}, []
    for name in gpt_astra.CODEX_TOLERATED_TOOLS:
        ns, tool = name.split(".", 1)
        if ns == "exec":
            nested.append(tool)
        else:
            by_ns.setdefault(ns, []).append(tool)
    return _trace(by_ns, nested)


def _preflight(monkeypatch, tmp_path, *, stderr, rollout_text, session="s1"):
    _official(monkeypatch)
    seen = {}
    rollout = tmp_path / "rollout.jsonl"
    if rollout_text is not None:
        rollout.write_text(rollout_text)

    def fake_meta(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env") or {}
        seen["cwd_empty"] = not any(Path(cmd[cmd.index("--cd") + 1]).iterdir())
        return {"session_id": session, "stderr": stderr, "result": "ok", "errors": None}
    monkeypatch.setattr(gpt_astra, "run_codex_meta", fake_meta)
    monkeypatch.setattr(gpt_astra, "_rollout_path",
                        lambda sid: rollout if rollout_text is not None else None)
    gpt_astra.official_session_preflight()
    return seen


def test_session_preflight_passes_on_the_tolerated_surface(monkeypatch, tmp_path):
    seen = _preflight(monkeypatch, tmp_path, stderr=_tolerated_trace(),
                      rollout_text=_rollout_lines(leaky=False))
    cmd = seen["cmd"]
    assert cmd[-1] == "reply ok" and seen["cwd_empty"]
    assert all(c in cmd for c in gpt_astra.CODEX_ISOLATION_CONFIG)
    assert any(a.startswith("model_instructions_file=") for a in cmd)
    assert "tungstenite::protocol=trace" in seen["env"]["RUST_LOG"]
    assert not Path(codex_loop.base_instructions_file(cmd)).exists()


def test_session_preflight_fails_closed_on_a_new_offered_tool(monkeypatch, tmp_path):
    trace = _trace({"functions": ["exec"]}, nested=["web__run"])
    with pytest.raises(SystemExit, match="exec.web__run"):
        _preflight(monkeypatch, tmp_path, stderr=trace, rollout_text=_rollout_lines(leaky=False))


def test_session_preflight_fails_closed_without_a_tool_list(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="offered"):
        _preflight(monkeypatch, tmp_path, stderr="", rollout_text=_rollout_lines(leaky=False))


def test_session_preflight_fails_closed_on_context_leaks(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="environment_context"):
        _preflight(monkeypatch, tmp_path, stderr=_tolerated_trace(),
                   rollout_text=_rollout_lines(leaky=True))


def test_session_preflight_fails_closed_without_the_rollout(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="rollout"):
        _preflight(monkeypatch, tmp_path, stderr=_tolerated_trace(), rollout_text=None)


def test_runner_preflight_adds_the_session_probe_only_under_official(monkeypatch):
    probes = []
    monkeypatch.setattr(gpt_astra.osworld_eval, "pinned_code_preflight", lambda: None)
    monkeypatch.setattr(gpt_astra, "_validate_campaign_lock", lambda: None)
    monkeypatch.setattr(gpt_astra.astra_common, "check_codex_cli", lambda **kw: None)
    monkeypatch.setattr(gpt_astra, "official_session_preflight", lambda: probes.append(1))
    monkeypatch.setattr(config, "OFFICIAL", False)
    gpt_astra.preflight()
    assert probes == []
    monkeypatch.setattr(config, "OFFICIAL", True)
    gpt_astra.preflight()
    assert probes == [1]


# --- the open-book runner is outside the official protocol ---

def test_openbook_preflight_refuses_the_official_protocol(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL", True)
    with patch.object(gpt_astra_openbook.open_book_preflight, "campaign_check") as check:
        with pytest.raises(SystemExit, match="OSW_PROTOCOL=official"):
            gpt_astra_openbook.preflight()
    assert not check.called
