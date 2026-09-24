"""Final-review fix wave, shared part (S1-S8): transcript recovery without a session id, MCP
liveness + step accounting, the exact official tool surface, the pinned Claude Code CLI, the
driver-verified live probes, and small protocol/provenance knobs. No real CLI is ever run."""
import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.osworld import config
from benchmarks.osworld.runners import agent_computer, common, gpt_astra
from core import run as core_run


class _FakeEnv:
    browser = None
    setup_error = None


TASK = {"id": "t", "instruction": "Do X", "evaluator": {}}


def _official(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setattr(config, "PROTOCOL", "official")
    monkeypatch.setattr(config, "INLOOP_VERIFY", False)


def _read(out, name):
    p = out / name
    return json.loads(p.read_text()) if p.exists() else None


def _lines(*events):
    return "\n".join(json.dumps(e) for e in events)


def _say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _computer_use():
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__osworld__computer", "input": {"action": "screenshot"}}]}}


def _snapshot(tools):
    return {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x"],
                                                 "tools": [{"name": t} for t in tools]}}


def _claude_run(monkeypatch, tmp_path, *, meta, on_call=None, mcp_state=True, home=None):
    """A non-dry official Claude run: fake CLI, real transcript lookup under a fake $HOME."""
    _official(monkeypatch)
    home = home or tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common.Path, "home", classmethod(lambda cls: home))
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    seen = {"scored": False}

    def fake_meta(cmd, **kw):
        seen["kw"] = kw
        if mcp_state:
            (out / "mcp_state.json").write_text(json.dumps(
                {"started": True, "steps_used": 7, "max_steps": config.MAX_STEPS}))
        if on_call:
            on_call(home, kw)
        return dict(meta)

    def fake_score(ctrl, task, answer, o):
        seen["scored"] = True
        return {"verdict": "FAILURE", "reward": 0.0}

    monkeypatch.setattr(agent_computer, "run_claude_meta", fake_meta)
    monkeypatch.setattr(agent_computer, "_score", fake_score)
    monkeypatch.setattr(agent_computer, "protocol_wait", lambda s: None)
    monkeypatch.setattr(agent_computer, "claude_cli_version", lambda **kw: "2.1.280")
    answer = agent_computer.run(TASK, env=_FakeEnv(), out=out)
    return answer, out, seen


# --- S1: transcript recovery when the envelope has no session_id ---

def test_project_dir_name_matches_claude_code_cwd_encoding():
    # Verified on disk (CLI 2.1.280): /private/var/folders/.../T/osw_claude__64k1ohw is stored
    # under ~/.claude/projects/-private-var-folders-...-T-osw-claude--64k1ohw -- every
    # non-alphanumeric character becomes '-'.
    assert common.claude_project_dir_name("/private/var/f/T/osw_claude__64k1ohw") == \
        "-private-var-f-T-osw-claude--64k1ohw"


def test_workdir_session_file_picks_the_newest_jsonl(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(common.Path, "home", classmethod(lambda cls: home))
    proj = home / ".claude" / "projects" / "-private-tmp-osw-claude-ab-c"
    proj.mkdir(parents=True)
    old, new = proj / "a.jsonl", proj / "b.jsonl"
    old.write_text("old")
    new.write_text("new")
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert common.claude_workdir_session_file("/tmp/osw_claude_ab.c") == new
    assert common.claude_workdir_session_file("/tmp/osw_claude_zz") is None
    assert common.claude_workdir_session_file(None) is None


def _write_session_for_cwd(lines):
    def on_call(home, kw):
        proj = home / ".claude" / "projects" / common.claude_project_dir_name(
            os.path.realpath(kw["cwd"]))
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "sess-9.jsonl").write_text(_lines(*lines))
    return on_call


def test_timeout_without_session_id_recovers_the_transcript_from_the_workdir(monkeypatch, tmp_path):
    answer, out, seen = _claude_run(
        monkeypatch, tmp_path, meta={},   # TASK_TIMEOUT kill: no parseable envelope
        on_call=_write_session_for_cwd([_computer_use(), _say("impossible [INFEASIBLE]"),
                                        _snapshot(["mcp__osworld__computer"])]))
    result = _read(out, "result.json")
    assert (out / "conversation.jsonl").exists()
    assert result["transcript_saved"] is True
    assert result["transcript_recovered_from_workdir"] is True
    assert answer == "FAIL" and result["answer"] == "FAIL"
    assert result["agent_non_computer_tool_calls"] == []
    assert result["agent_context_leaks"] == []
    assert seen["scored"] and _read(out, "eval.json") is not None
    assert _read(out, "infra_error.json") is None


def test_unrecoverable_transcript_is_an_unscored_tool_audit_error(monkeypatch, tmp_path):
    answer, out, seen = _claude_run(monkeypatch, tmp_path, meta={})
    infra = _read(out, "infra_error.json")[-1]
    assert infra["outcome"] == "HARNESS_ERROR" and infra["error_type"] == "ToolAuditUnavailable"
    assert _read(out, "eval.json") is None and not seen["scored"]
    assert _read(out, "result.json")["agent_non_computer_tool_calls"] is None
    assert answer == ""


# --- S2: MCP liveness + step accounting ---

def test_mcp_child_env_names_the_state_file_only_when_out_dir_is_given(tmp_path):
    assert common.mcp_child_env(tmp_path)["OSW_MCP_STATE_FILE"] == str(tmp_path / "mcp_state.json")
    assert "OSW_MCP_STATE_FILE" not in common.mcp_child_env()


def _spec(path):
    try:
        return json.loads(Path(path).read_text())["mcpServers"]["osworld"]
    finally:
        os.unlink(path)


def test_official_claude_mcp_config_uses_this_interpreter_and_the_state_file(tmp_path):
    spec = _spec(common._mcp_config("http://x", tmp_path))
    assert spec["command"] == sys.executable
    assert spec["env"]["OSW_MCP_STATE_FILE"] == str(tmp_path / "mcp_state.json")


def test_computer_session_state_file(tmp_path):
    from benchmarks.osworld.mcp import official_computer
    state = tmp_path / "s.json"
    official_computer.write_state(str(state), steps_used=3, max_steps=100)
    assert json.loads(state.read_text()) == {"started": True, "steps_used": 3, "max_steps": 100}
    official_computer.write_state("", steps_used=3, max_steps=100)   # unset: no-op


def test_official_server_writes_state_at_startup_and_after_each_call(tmp_path):
    import base64, io   # noqa: E401
    from PIL import Image
    from benchmarks.osworld.tests.test_official_mcp import _reset, _server
    state = tmp_path / "mcp_state.json"
    s = _server(OSW_PROTOCOL="official", OSW_MAX_STEPS="5", OSW_MCP_STATE_FILE=str(state))
    try:
        assert json.loads(state.read_text()) == {"started": True, "steps_used": 0, "max_steps": 5}
        buf = io.BytesIO()
        Image.new("RGB", (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)).save(buf, format="PNG")
        s._ctrl.screenshot = lambda: buf.getvalue()
        s._ctrl.execute = lambda *a, **k: '{"status": "success"}'
        s._session.sleep = lambda _: None
        asyncio.run(s.mcp.call_tool("computer", {"action": "left_click", "coordinate": [1, 2]}))
        assert json.loads(state.read_text())["steps_used"] == 1
    finally:
        _reset()


def test_claude_run_records_steps_and_passes_the_state_file(monkeypatch, tmp_path):
    captured = {}
    real = common._mcp_config

    def spy(url, out_dir=None):
        captured["out_dir"] = out_dir
        return real(url, out_dir)
    monkeypatch.setattr(agent_computer, "_mcp_config", spy)
    _, out, seen = _claude_run(monkeypatch, tmp_path, meta={"session_id": None},
                               on_call=_write_session_for_cwd([_computer_use()]))
    assert captured["out_dir"] == out
    assert _read(out, "result.json")["agent_steps_used"] == 7


def test_claude_run_without_the_mcp_state_is_an_unscored_harness_error(monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "mcp_state.json").write_text('{"started": true, "steps_used": 3}')   # stale
    (out / "eval.json").write_text('{"verdict": "SUCCESS"}')   # stale, from an earlier attempt
    _, out, seen = _claude_run(monkeypatch, tmp_path, meta={}, mcp_state=False,
                               on_call=_write_session_for_cwd([_computer_use()]))
    infra = _read(out, "infra_error.json")[-1]
    assert infra["outcome"] == "HARNESS_ERROR" and infra["error_type"] == "McpServerUnavailable"
    assert _read(out, "eval.json") is None and not seen["scored"]
    assert _read(out, "result.json")["agent_steps_used"] is None


def _codex_run(monkeypatch, tmp_path, *, mcp_state=True):
    from benchmarks.osworld.tests.test_official_codex_runner import _computer, _rollout_lines
    _official(monkeypatch)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_rollout_lines(leaky=False))
    events = [_computer(1, {"action": "screenshot"})]
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    (out / "mcp_state.json").write_text('{"started": true, "steps_used": 1}')   # stale
    seen = {"scored": False}

    def fake_meta(cmd, **kw):
        seen["cmd"] = cmd
        if mcp_state:
            (out / "mcp_state.json").write_text(json.dumps(
                {"started": True, "steps_used": 4, "max_steps": config.MAX_STEPS}))
        return {"result": "", "session_id": "s1", "usage": {}, "errors": None,
                "is_error": False, "timed_out": False, "returncode": 0, "num_turns": 1,
                "tool_calls": 1, "mcp_tool_calls": 1, "non_mcp_tool_calls": 0,
                "tool_names": ["computer"], "events": events,
                "raw": _lines(*events) + "\n", "stderr": ""}

    def fake_score(ctrl, task, answer, o):
        seen["scored"] = True
        return {"verdict": "FAILURE", "reward": 0.0}
    monkeypatch.setattr(gpt_astra, "run_codex_meta", fake_meta)
    monkeypatch.setattr(gpt_astra, "_codex_version", lambda: "codex-cli 0.153.4")
    monkeypatch.setattr(gpt_astra, "_score", fake_score)
    monkeypatch.setattr(gpt_astra, "protocol_wait", lambda s: None)
    monkeypatch.setattr(gpt_astra, "_rollout_path", lambda sid: rollout)
    gpt_astra.run(TASK, env=_FakeEnv(), out=out)
    return out, seen


def test_codex_run_records_steps_and_passes_the_state_file(monkeypatch, tmp_path):
    out, seen = _codex_run(monkeypatch, tmp_path)
    assert f'OSW_MCP_STATE_FILE = {json.dumps(str(out / "mcp_state.json"))}' in " ".join(seen["cmd"])
    assert _read(out, "result.json")["agent_steps_used"] == 4
    assert seen["scored"]


def test_codex_run_without_the_mcp_state_is_an_unscored_harness_error(monkeypatch, tmp_path):
    out, seen = _codex_run(monkeypatch, tmp_path, mcp_state=False)
    infra = _read(out, "infra_error.json")[-1]
    assert infra["outcome"] == "HARNESS_ERROR" and infra["error_type"] == "McpServerUnavailable"
    assert _read(out, "eval.json") is None and not seen["scored"]


# --- S3: only the official tool may be offered ---

def _probe(monkeypatch, tmp_path, lines, tools):
    from benchmarks.osworld.tests.test_official_claude_runner import _probe as probe
    return probe(monkeypatch, tmp_path, lines, tools=tools)


def _clean():
    from benchmarks.osworld.tests.test_official_claude_runner import _clean_lines
    return _clean_lines("/private/tmp/x")


def test_tool_preflight_refuses_a_foreign_mcp_tool_in_init(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _clean() + [_snapshot([])],
           tools=[*agent_computer.CLAUDE_BUILTIN_TOOLS, "mcp__playwright__browser_click"])
    with pytest.raises(SystemExit, match="mcp__playwright__browser_click"):
        agent_computer.official_tool_preflight()


def test_tool_preflight_refuses_a_foreign_mcp_tool_offered_to_the_model(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _clean() + [_snapshot(["mcp__other__tool"])],
           tools=agent_computer.CLAUDE_BUILTIN_TOOLS)
    with pytest.raises(SystemExit, match="mcp__other__tool"):
        agent_computer.official_tool_preflight()


def test_tool_preflight_accepts_the_official_tool(monkeypatch, tmp_path):
    _probe(monkeypatch, tmp_path, _clean() + [_snapshot(["mcp__osworld__computer"])],
           tools=[*agent_computer.CLAUDE_BUILTIN_TOOLS, "mcp__osworld__computer"])
    agent_computer.official_tool_preflight()


def test_run_records_unexpected_offered_tools(monkeypatch, tmp_path):
    _, out, _ = _claude_run(monkeypatch, tmp_path, meta={"session_id": None},
                            on_call=_write_session_for_cwd(
                                [_snapshot(["mcp__osworld__computer", "mcp__x__y", "Glob"])]))
    assert _read(out, "result.json")["agent_unexpected_offered_tools"] == ["mcp__x__y", "Glob"]


# --- S4: pinned Claude Code CLI ---

def test_claude_code_version_default_and_override():
    with patch.dict(os.environ, {"OSW_CLAUDE_CODE_VERSION": ""}):
        os.environ.pop("OSW_CLAUDE_CODE_VERSION")
        assert importlib.reload(config).CLAUDE_CODE_VERSION == "2.1.280"
    with patch.dict(os.environ, {"OSW_CLAUDE_CODE_VERSION": "9.9.9"}):
        assert importlib.reload(config).CLAUDE_CODE_VERSION == "9.9.9"
    importlib.reload(config)


def test_claude_env_disables_the_autoupdater(monkeypatch):
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", None)
    assert common.claude_env()["DISABLE_AUTOUPDATER"] == "1"


def test_claude_cli_version_parses_and_uses_the_run_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="2.1.280 (Claude Code)\n", stderr="")
    monkeypatch.setattr(common.subprocess, "run", fake_run)
    assert common.claude_cli_version(cached=False) == "2.1.280"
    assert seen["cmd"] == ["claude", "--version"]
    assert seen["env"]["DISABLE_AUTOUPDATER"] == "1"

    def boom(cmd, **kw):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(common.subprocess, "run", boom)
    assert common.claude_cli_version(cached=False) is None


def test_version_preflight_refuses_a_mismatch(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_CODE_VERSION", "2.1.280")
    monkeypatch.setattr(agent_computer, "claude_cli_version", lambda **kw: "2.1.281")
    with pytest.raises(SystemExit) as e:
        agent_computer.claude_version_preflight()
    assert "2.1.280" in str(e.value) and "2.1.281" in str(e.value)
    monkeypatch.setattr(agent_computer, "claude_cli_version", lambda **kw: None)
    with pytest.raises(SystemExit):
        agent_computer.claude_version_preflight()
    monkeypatch.setattr(agent_computer, "claude_cli_version", lambda **kw: "2.1.280")
    agent_computer.claude_version_preflight()


def test_tool_preflight_probe_runs_with_the_run_env(monkeypatch, tmp_path):
    seen = {}
    from benchmarks.osworld.tests.test_official_claude_runner import _clean_lines, _write
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    monkeypatch.setattr(common.Path, "home", classmethod(lambda cls: home))

    def fake_raw(cmd, *, timeout, env=None, cwd=None):
        seen["env"] = env
        _write(proj / "sid-1.jsonl", _clean_lines("/private/tmp/x") + [_snapshot([])])
        return json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1",
                           "tools": agent_computer.CLAUDE_BUILTIN_TOOLS})
    monkeypatch.setattr(agent_computer, "_run_raw", fake_raw)
    agent_computer.official_tool_preflight()
    assert seen["env"]["DISABLE_AUTOUPDATER"] == "1"


def test_official_run_records_the_cli_version_and_env(monkeypatch, tmp_path):
    _, out, seen = _claude_run(monkeypatch, tmp_path, meta={"session_id": None},
                               on_call=_write_session_for_cwd([_computer_use()]))
    prov = _read(out, "result.json")["provenance"]
    assert prov["agent_runtime"] == "claude_code" and prov["agent_runtime_version"] == "2.1.280"
    assert seen["kw"]["env"]["DISABLE_AUTOUPDATER"] == "1"


# --- S5: children skip only the live-model probe when the driver already ran it ---

def _preflight_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    monkeypatch.setattr(agent_computer.osworld_eval, "pinned_code_preflight",
                        lambda: calls.append("pinned"))
    monkeypatch.setattr(agent_computer, "claude_version_preflight", lambda: calls.append("version"))
    monkeypatch.setattr(agent_computer, "official_tool_preflight", lambda: calls.append("tools"))
    return calls


@pytest.mark.parametrize("ok,run_id,probed", [
    ("drv-1", "drv-1", False), ("drv-1", "drv-2", True), ("", "", True), ("drv-1", "", True),
])
def test_claude_preflight_skips_only_the_probe_for_the_driver_run(monkeypatch, ok, run_id, probed):
    calls = _preflight_calls(monkeypatch)
    monkeypatch.setenv("OSW_OFFICIAL_PREFLIGHT_OK", ok)
    monkeypatch.setenv("OSW_KVM_DRIVER_RUN", run_id)
    agent_computer.preflight()
    assert calls == ["pinned", "version"] + (["tools"] if probed else [])


def test_codex_preflight_skips_only_the_session_probe_for_the_driver_run(monkeypatch):
    calls = []
    monkeypatch.setattr(gpt_astra.osworld_eval, "pinned_code_preflight", lambda: calls.append("pinned"))
    monkeypatch.setattr(gpt_astra, "_validate_campaign_lock", lambda: calls.append("lock"))
    monkeypatch.setattr(gpt_astra.astra_common, "check_codex_cli", lambda **kw: calls.append("cli"))
    monkeypatch.setattr(gpt_astra, "official_session_preflight", lambda: calls.append("probe"))
    monkeypatch.setattr(config, "OFFICIAL", True)
    monkeypatch.setenv("OSW_OFFICIAL_PREFLIGHT_OK", "drv-1")
    monkeypatch.setenv("OSW_KVM_DRIVER_RUN", "drv-1")
    gpt_astra.preflight()
    assert calls == ["pinned", "lock", "cli"]
    monkeypatch.setenv("OSW_KVM_DRIVER_RUN", "other")
    gpt_astra.preflight()
    assert calls[-1] == "probe"


# --- S6/S7/S8 ---

def _reload_max_steps(**env):
    base = {"OSW_PROTOCOL": "", "OSW_BACKEND": "", "OSW_TASK_TIMEOUT": ""}
    base.update(env)
    with patch.dict(os.environ, base):
        if "OSW_MAX_STEPS" not in env:
            os.environ.pop("OSW_MAX_STEPS", None)
        value = importlib.reload(config).MAX_STEPS
    importlib.reload(config)
    return value


def test_max_steps_defaults_to_100_under_official_only():
    assert _reload_max_steps() == 30
    assert _reload_max_steps(OSW_PROTOCOL="official") == 100
    assert _reload_max_steps(OSW_PROTOCOL="official", OSW_MAX_STEPS="42") == 42


def test_provenance_image_is_the_kvm_image_on_kvm(monkeypatch):
    monkeypatch.setattr(config, "KVM_IMAGE", "happysixd/osworld-docker@sha256:abc")
    monkeypatch.setattr(config, "BACKEND", "kvm")
    assert common._provenance({"id": "t"}, None, "x")["image"] == config.KVM_IMAGE
    monkeypatch.setattr(config, "BACKEND", "daytona")
    assert common._provenance({"id": "t"}, None, "x")["image"] == config.IMAGE


def test_client_password_is_a_recorded_harness_knob():
    assert "OSW_KVM_CLIENT_PASSWORD" in core_run._HARNESS_ENV_KEYS
