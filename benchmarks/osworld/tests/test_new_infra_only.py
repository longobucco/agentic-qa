"""The new-infrastructure branches carry only the official protocol: the legacy systems live in
the tag phase1-daytona-frozen."""
import importlib
import pathlib
import re

import pytest

from benchmarks.osworld import benchmark

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BANNED = re.compile(r"config\.OFFICIAL|OSW_ZOOM_BATCH|OSW_GROUNDING|OSW_INLOOP_VERIFY|"
                     r"OSW_SELF_VERIFY|OSW_ENFORCE_SANDBOX|OSW_RESTRICT_RUN_PYTHON|OSW_VR_|"
                     r"verify_replan|openbook|open_book|agent_prompt")
# Files allowed to name the legacy knobs: the refusal list itself, this test, and the campaign
# core's env-conflict table (and its tests).
_ALLOWED = {"benchmarks/osworld/config.py", "benchmarks/osworld/tests/test_new_infra_only.py",
            "benchmarks/osworld/campaign.py", "benchmarks/osworld/tests/test_campaign.py",
            "benchmarks/osworld/tests/test_campaign_kvm.py"}


def test_no_source_file_mentions_the_legacy_harness():
    hits = []
    for base in ("benchmarks/osworld", "core", "scripts"):
        for p in (_ROOT / base).rglob("*.py"):
            rel = p.relative_to(_ROOT).as_posix()
            if rel in _ALLOWED or "/data/" in rel or "/results/" in rel:
                continue
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if _BANNED.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []


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


def test_claude_preflight_refuses_a_legacy_knob_set_after_import(monkeypatch):
    """core.run loads .env after benchmark.build() imports config, so a legacy knob set late
    (e.g. by a stale .env) must still be caught -- the preflight re-checks it directly rather
    than relying only on the import-time refusal."""
    from benchmarks.osworld import config
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OSW_VR_MAX_RECOVERIES", "1")
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        agent_computer.preflight()


def test_codex_preflight_refuses_a_legacy_knob_set_after_import(monkeypatch):
    from benchmarks.osworld import config
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setenv("OSW_VR_MAX_RECOVERIES", "1")
    # stub every step after the refusal so a regression that removes the early check doesn't
    # fail here for the wrong reason (a missing lock file, network call, etc).
    monkeypatch.setattr("benchmarks.osworld.env.osworld_eval.pinned_code_preflight",
                        lambda: (_ for _ in ()).throw(AssertionError("should not run")))
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        gpt_astra.preflight()


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


PHASE1_TREES = [
    "agent_computer", "agent_computer_astra", "agent_computer_astra_broadcanary20260922",
    "agent_computer_astra_broadcanary20260922_openbook", "agent_computer_astra_fixedimg",
    "agent_computer_astra_infracanary20260921", "agent_computer_astra_openbook",
    "agent_computer_astra_openbookcanarynautilus20260922_openbook",
    "agent_computer_astra_openbookfailuresrecheck20260922_openbook",
    "agent_computer_astra_probe", "agent_computer_astra_quotaprobe20260922",
    "agent_computer_astra_vlccdpvalidation20260923",
    "agent_computer_astra_xdgcdpvalidation20260923", "agent_computer_sonnet5",
    "verify_replan_sonnet5", "verify_replan_sonnet5_auditonly", "_probes",
]


@pytest.mark.parametrize("name", PHASE1_TREES)
def test_every_phase1_tree_is_refused(name):
    from benchmarks.osworld import config
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        config.assert_new_infra_system(name)


@pytest.mark.parametrize("name", [
    "agent_computer_sonnet5_effortmax_canary20260924_official",
    "agent_computer_gpt6astra_max_codex01534_canary20260924_official",
    "agent_computer_sonnet5_effortmax_protocol361_official_kvm",
])
def test_new_infra_trees_are_accepted(name):
    from benchmarks.osworld import config
    assert config.assert_new_infra_system(name) == name


def test_sonnet_canary_tree_name_is_stable(monkeypatch):
    config = _reload_config(monkeypatch, OSW_MODEL="claude-sonnet-5", OSW_EFFORT="max",
                            OSW_SYSTEM_SUFFIX="canary20260924")
    try:
        assert config.SYSTEM_NAME == "agent_computer_sonnet5_effortmax_canary20260924_official"
    finally:
        _reload_config(monkeypatch)


def test_neutral_legacy_values_are_accepted(monkeypatch):
    """Review Focus 1: an inherited shell/MCP child env with 'off' values still starts."""
    config = _reload_config(monkeypatch, OSW_ZOOM_BATCH="0", OSW_PINNED_EVALUATORS="1",
                            OSW_PROTOCOL="official", OSW_POPULATION="verified361")
    try:
        assert config.PROTOCOL == "official" and config.POPULATION == "verified361"
    finally:
        _reload_config(monkeypatch)


@pytest.mark.parametrize("env", [
    {"OSW_ZOOM_BATCH": "1"}, {"OSW_GROUNDING": "1"}, {"OSW_INLOOP_VERIFY": "1"},
    {"OSW_VR_MAX_RECOVERIES": "0"}, {"OSW_PINNED_EVALUATORS": "0"}, {"OSW_MAX_TURNS": "50"},
])
def test_active_legacy_knobs_are_refused(env):
    from benchmarks.osworld import config
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        config._refuse_legacy_knobs(env)


@pytest.mark.parametrize("env", [{"OSW_PROTOCOL": "legacy"}, {"OSW_POPULATION": "all"},
                                 {"OSW_RELEASE": "full"}])
def test_non_official_protocol_or_population_is_refused(monkeypatch, env):
    with pytest.raises(SystemExit):
        _reload_config(monkeypatch, **env)
    _reload_config(monkeypatch)


def test_build_refuses_a_phase1_runner_name(monkeypatch):
    from benchmarks.osworld import config
    monkeypatch.setattr(config, "SYSTEM_NAME", "agent_computer_sonnet5")
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        benchmark.build()


def test_report_defaults_to_the_current_tree_and_refuses_phase1(monkeypatch, capsys):
    """Review Focus 5."""
    import sys
    from benchmarks.osworld import config, report
    monkeypatch.setattr(sys, "argv", ["report", "agent_computer"])
    with pytest.raises(SystemExit, match="phase1-daytona-frozen"):
        report.main()
    seen = []
    monkeypatch.setattr(report, "summarize", lambda root, system, title: seen.append(system))
    for name in ("_source_breakdown", "_mean_reward_line", "_incidental_breakdown",
                 "_tool_surface_violation_line"):
        monkeypatch.setattr(report, name, lambda system: None)
    monkeypatch.setattr(sys, "argv", ["report"])
    report.main()
    assert seen == [config.SYSTEM_NAME]
