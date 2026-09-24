"""The new-infrastructure branches carry only the official protocol: the legacy systems live in
the tag phase1-daytona-frozen."""
import importlib

import pytest

from benchmarks.osworld import benchmark


def test_claude_runner_builds_the_official_command_without_the_protocol_flag(monkeypatch, tmp_path):
    from benchmarks.osworld import config
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    captured = {}
    monkeypatch.setattr(agent_computer, "build_claude_cmd",
                        lambda prompt, **kw: captured.update(prompt=prompt, **kw) or ["claude"])
    task = {"id": "t1", "instruction": "Open the file.", "related_apps": ["os"]}
    agent_computer.run(task, env=None, out=tmp_path, dry=True)
    assert captured["prompt"] == "Open the file."          # bare instruction, no legacy prompt
    assert captured["allowed_tools"] == ["mcp__osworld__computer"]
    assert "--strict-mcp-config" in captured["extra"]


def test_claude_preflight_refuses_an_unpinned_model(monkeypatch):
    from benchmarks.osworld import config
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "MODEL", "")
    with pytest.raises(SystemExit, match="OSW_MODEL is required"):
        agent_computer.preflight()


def test_provenance_records_the_official_observation_and_action_space():
    from benchmarks.osworld.runners import common
    prov = common._provenance({"id": "t1", "related_apps": []}, None, "2026-09-24T00:00:00Z")
    assert prov["protocol"] == "official"
    assert prov["observation"] == "screenshot"
    assert prov["action_space"] == "computer_20251124"
    assert prov["max_turns"] == common.official_max_turns()

REMOVED_MODULES = [
    "benchmarks.osworld.runners.verify_replan",
    "benchmarks.osworld.runners.gpt_astra_openbook",
    "benchmarks.osworld.verification",
    "benchmarks.osworld.closed_book",
    "benchmarks.osworld.open_book_preflight",
    "benchmarks.osworld.openbook_proxy",
    "benchmarks.osworld.env.guest_proxy",
    "benchmarks.osworld.env.host_proxy",
    "benchmarks.osworld.mcp.readonly_server",
]


@pytest.mark.parametrize("name", REMOVED_MODULES)
def test_legacy_module_is_gone(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


def test_only_the_two_official_runners_are_registered():
    from benchmarks.osworld import config
    assert set(benchmark.build().runners) == {config.SYSTEM_NAME, config.ASTRA_SYSTEM_NAME}


def test_mcp_server_offers_only_computer_without_any_protocol_env(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.delenv("OSW_PROTOCOL", raising=False)
    monkeypatch.setenv("OSW_MCP_STATE_FILE", str(tmp_path / "mcp_state.json"))
    from benchmarks.osworld.mcp import server
    server = importlib.reload(server)
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert names == ["computer"]


def test_codex_allowed_mcp_tools_is_computer_only():
    from core.codex_loop import allowed_mcp_tools
    assert allowed_mcp_tools() == ("computer",)


@pytest.mark.parametrize("name", [
    "benchmarks.osworld.mcp.grounding_tools", "benchmarks.osworld.mcp.zoom_batch_tools",
    "benchmarks.osworld.grounding",
])
def test_legacy_mcp_channel_is_gone(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


def _reload_config(monkeypatch, **env):
    for k in [k for k in __import__("os").environ if k.startswith("OSW_")]:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from benchmarks.osworld import config
    return importlib.reload(config)


def test_astra_canary_tree_name_is_stable(monkeypatch):
    config = _reload_config(monkeypatch, OSW_ASTRA_REASONING_EFFORT="max",
                            OSW_ASTRA_SYSTEM_SUFFIX="canary20260924")
    try:
        assert config.ASTRA_SYSTEM_NAME == \
            "agent_computer_gpt6astra_max_codex01534_canary20260924_official"
    finally:
        _reload_config(monkeypatch)


def test_astra_defaults_are_the_official_campaign(monkeypatch):
    config = _reload_config(monkeypatch)
    try:
        assert config.ASTRA_REASONING_EFFORT == "max"
        assert config.ASTRA_CAMPAIGN_LOCK.name == "astra_official361_lock.json"
        assert config.ASTRA_SYSTEM_NAME == "agent_computer_gpt6astra_max_codex01534_official"
    finally:
        _reload_config(monkeypatch)


def test_codex_non_mcp_tool_call_is_a_terminal_failure(monkeypatch, tmp_path):
    """Review Focus 4: Codex reports non-MCP calls while the official audit list is empty."""
    import json
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "run_codex_meta", lambda cmd, timeout: {
        "raw": '{"type":"thread.started"}\n', "stderr": "", "events": [], "result": "",
        "session_id": "s1", "non_mcp_tool_calls": 1, "tool_names": ["command_execution"]})
    monkeypatch.setattr(gpt_astra, "_official_audit", lambda *a: {
        "agent_non_computer_tool_calls": [], "agent_context_leaks": [],
        "agent_offered_tools": None})
    monkeypatch.setattr(gpt_astra, "read_mcp_state",
                        lambda out: {"started": True, "steps_used": 3, "max_steps": 100})
    monkeypatch.setattr(gpt_astra, "protocol_wait", lambda s: None)
    monkeypatch.setattr(gpt_astra, "_codex_version", lambda: "0.153.4")
    monkeypatch.setattr(gpt_astra, "_write_tool_audit",
                        lambda out, events: {"contaminated": False, "evidence": []})
    task = {"id": "t1", "instruction": "Do it.", "related_apps": ["os"]}
    gpt_astra.run(task, env=None, out=tmp_path)
    ev = json.loads((tmp_path / "eval.json").read_text())
    assert ev["verdict"] == "FAILURE" and ev["reward"] == 0.0
    assert ev["tool_surface_violation"] == ["command_execution"]
    assert not (tmp_path / "infra_error.json").exists()
