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
