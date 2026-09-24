"""The new-infrastructure branches carry only the official protocol: the legacy systems live in
the tag phase1-daytona-frozen."""
import importlib

import pytest

from benchmarks.osworld import benchmark

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
