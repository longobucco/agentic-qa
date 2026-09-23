import importlib
import os
from unittest.mock import patch

from benchmarks.osworld import config
from benchmarks.osworld.runners import common
from core.agent_loop import build_claude_cmd


def test_effort_flag_is_added_only_when_set():
    assert "--effort" not in build_claude_cmd("p")
    cmd = build_claude_cmd("p", effort="max")
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_claude_env_sets_output_tokens_only_when_configured(monkeypatch):
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", None)
    assert common.claude_env() is None
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 128000)
    env = common.claude_env()
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
    assert env["PATH"] == os.environ["PATH"]   # a full environment, not a replacement


def test_provenance_records_protocol_fields(monkeypatch):
    monkeypatch.setattr(config, "EFFORT", "max")
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 128000)
    rec = common._provenance({"id": "t"}, ctrl=None, started_at="x")
    assert rec["effort"] == "max"
    assert rec["max_output_tokens"] == 128000
    assert rec["max_steps"] == config.MAX_STEPS
    assert rec["screen_size"] == f"{config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}"


def test_effort_changes_the_results_tree_name():
    with patch.dict(os.environ, {"OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "",
                                 "OSW_SYSTEM_SUFFIX": ""}):
        base = importlib.reload(config).SYSTEM_NAME
    with patch.dict(os.environ, {"OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
                                 "OSW_SYSTEM_SUFFIX": ""}):
        with_effort = importlib.reload(config).SYSTEM_NAME
    importlib.reload(config)
    assert base == "agent_computer_sonnet5"
    assert with_effort == "agent_computer_sonnet5_effortmax"


def test_runner_passes_effort_and_env_only_when_set(monkeypatch, tmp_path):
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "EFFORT", "")
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", None)
    assert agent_computer._effort_kwargs() == {} and agent_computer._env_kwargs() == {}
    monkeypatch.setattr(config, "EFFORT", "max")
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 128000)
    assert agent_computer._effort_kwargs() == {"effort": "max"}
    assert agent_computer._env_kwargs()["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
