import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from benchmarks.osworld import arms, config
from benchmarks.osworld.env.controller import Controller
from benchmarks.osworld.mcp import command_tool
from benchmarks.osworld.mcp.official_computer import ComputerSession
from benchmarks.osworld.runners import agent_computer, astra_common, common, gpt_astra


class FakeCtrl:
    def __init__(self, rec=None, exc=None):
        self.rec, self.exc, self.calls = rec, exc, []

    def run(self, command, *, timeout):
        self.calls.append((command, timeout))
        if self.exc:
            raise self.exc
        return self.rec

    def screenshot(self):
        return b""


def _session(steps=0, max_steps=100):
    s = ComputerSession(FakeCtrl(), max_steps=max_steps, sleep=lambda _: None)
    s.steps_used = steps
    return s


def test_command_runs_in_the_guest_and_counts_one_step(tmp_path):
    ctrl = FakeCtrl({"status": "success", "output": "1.25\n", "error": "", "returncode": 0})
    s, log = _session(steps=3), tmp_path / "commands.jsonl"
    out = command_tool.run_command(s, ctrl, "gsettings get x y", 30, log_path=str(log))
    assert ctrl.calls == [(["timeout", "--kill-after=5", "30", "bash", "-c", "gsettings get x y"], 45)]
    assert s.steps_used == 4
    assert "exit code: 0" in out and "1.25" in out and "[Current step: 4/100]" in out
    rec = json.loads(log.read_text())
    assert rec["command"] == "gsettings get x y" and rec["returncode"] == 0 and rec["step"] == 4


def test_timeout_is_clamped_and_reported():
    ctrl = FakeCtrl({"status": "success", "output": "", "error": "", "returncode": 124})
    out = command_tool.run_command(_session(), ctrl, "sleep 999", 5000)
    assert ctrl.calls[0][0][2] == str(command_tool.MAX_TIMEOUT_S)
    assert "Timed out after 110 s" in out


def test_errors_are_observations_not_exceptions():
    out = command_tool.run_command(_session(), FakeCtrl(exc=TimeoutError("read timed out")), "x")
    assert out.startswith("Error: TimeoutError") and "[Current step: 1/100]" in out
    out = command_tool.run_command(_session(), FakeCtrl({"status": "error", "message": "boom"}), "x")
    assert out.startswith("Error: boom")
    assert command_tool.run_command(_session(), FakeCtrl(), "  ").startswith("Error: command")


def test_budget_is_shared_with_computer():
    ctrl = FakeCtrl({"status": "success", "output": "", "error": "", "returncode": 0})
    s = _session(steps=100)
    assert command_tool.run_command(s, ctrl, "ls").startswith("Step limit reached")
    assert ctrl.calls == [] and s.steps_used == 100


def test_long_output_is_capped():
    big = "a" * (command_tool.MAX_CHARS + 50)
    ctrl = FakeCtrl({"status": "success", "output": big, "error": "", "returncode": 0})
    assert "[... 50 more characters cut]" in command_tool.run_command(_session(), ctrl, "cat f")


def test_controller_run_never_resends_after_an_http_error():
    err = urllib.error.HTTPError("http://g/execute", 500, "x", {},
                                 io.BytesIO(b'{"status": "error", "message": "timed out"}'))
    with patch("urllib.request.urlopen", side_effect=err) as op:
        rec = Controller("http://g").run(["ls"], timeout=5)
    assert op.call_count == 1 and rec == {"status": "error", "message": "timed out"}


def test_arm_declares_its_tool_and_card():
    assert arms.tools("code") == ("run_command",)
    assert arms.tools("") == () and arms.tools("e1card2") == ()
    assert arms.addendum("code").startswith(arms.E1C2_CARD) and "run_command" in arms.addendum("code")
    assert arms.provenance("code")["arm_tools"] == ["run_command"]
    assert "arm_tools" not in arms.provenance("e1card2")
    with pytest.raises(SystemExit):
        arms.tools("codetypo")


def test_mcp_env_enables_the_tool_only_under_the_arm(tmp_path):
    assert common.arm_tool_env("code") == {"OSW_ARM_TOOLS": "run_command"}
    assert common.arm_tool_env("") == {}
    assert common.mcp_child_env(tmp_path)["OSW_COMMAND_LOG"] == str(tmp_path / "commands.jsonl")


def test_claude_allows_run_command_only_under_the_arm(monkeypatch):
    monkeypatch.setattr(config, "ARM", "code")
    assert agent_computer.allowed_tools() == ["mcp__osworld__computer", "mcp__osworld__run_command"]
    monkeypatch.setattr(config, "ARM", "")
    assert agent_computer.allowed_tools() == ["mcp__osworld__computer"]


def test_codex_audit_accepts_run_command_only_under_the_arm(monkeypatch):
    ev = [{"type": "item.completed", "item": {"id": "1", "type": "mcp_tool_call", "tool": "run_command"}}]
    monkeypatch.setattr(config, "ASTRA_ARM", "code")
    assert gpt_astra._non_computer_tool_calls(ev) == []
    monkeypatch.setattr(config, "ASTRA_ARM", "")
    assert gpt_astra._non_computer_tool_calls(ev) == ["run_command"]
    code = "await tools.mcp__osworld__run_command({command: 'ls'})"
    assert astra_common._exec_tool_refs(code, ("run_command",)) == []
    assert astra_common._exec_tool_refs(code) == ["mcp__osworld__run_command"]


def _server_tools(env):
    import asyncio
    import importlib
    import os
    with patch.dict(os.environ, {"OSW_MCP_STATE_FILE": "", **env}):
        import benchmarks.osworld.mcp.server as server
        server = importlib.reload(server)
        return sorted(t.name for t in asyncio.run(server.mcp.list_tools()))


def test_server_registers_run_command_only_when_enabled():
    assert _server_tools({"OSW_ARM_TOOLS": ""}) == ["computer"]
    assert _server_tools({"OSW_ARM_TOOLS": "run_command"}) == ["computer", "run_command"]
