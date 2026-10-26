"""Phase 3 intervention arms: an arm only adds to the official prompt, is named in its own results
tree, is recorded in provenance, and an unknown arm name is refused."""
import hashlib
import os
from pathlib import Path

import pytest

from benchmarks.osworld import arms, config
from benchmarks.osworld.runners import gpt_astra
from core import codex_loop


def test_no_arm_leaves_the_official_prompt_untouched():
    assert arms.system_prompt("OFFICIAL", "") == "OFFICIAL"
    assert arms.provenance("") == {"arm": None, "arm_addendum_sha256": None}


def test_the_card_arm_appends_its_text_after_the_official_prompt():
    prompt = arms.system_prompt("OFFICIAL", "e1card")
    assert prompt == "OFFICIAL\n\n" + arms.E1_CARD
    assert "gsettings" in arms.E1_CARD and "[INFEASIBLE]" in arms.E1_CARD


def test_provenance_records_the_arm_and_a_hash_of_its_text():
    rec = arms.provenance("e1card")
    assert rec == {"arm": "e1card",
                   "arm_addendum_sha256": hashlib.sha256(arms.E1_CARD.encode()).hexdigest()}


def test_an_unknown_arm_is_refused():
    with pytest.raises(SystemExit):
        arms.addendum("e1crad")


def test_the_arm_name_goes_into_the_results_tree(monkeypatch):
    monkeypatch.setattr(config, "ASTRA_SYSTEM_SUFFIX", "pkgfix3")
    monkeypatch.setattr(config, "ASTRA_ARM", "e1card")
    name = config.astra_system_name()
    assert name.endswith("_pkgfix3_e1card_official")
    config.assert_new_infra_system(name)   # still a legal new-infrastructure tree name


def test_without_an_arm_the_tree_name_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "ASTRA_SYSTEM_SUFFIX", "pkgfix3")
    monkeypatch.setattr(config, "ASTRA_ARM", "")
    assert config.astra_system_name().endswith("_pkgfix3_official")


def test_the_codex_command_carries_the_card_in_its_instructions_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ASTRA_ARM", "e1card")
    cmd = gpt_astra._official_cmd("do it", tmp_path, "")
    path = codex_loop.base_instructions_file(cmd)
    try:
        text = Path(path).read_text()
        assert text.startswith(gpt_astra._official_system_prompt())
        assert text.endswith(arms.E1_CARD)
    finally:
        os.unlink(path)


def test_the_plain_codex_command_has_exactly_the_official_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ASTRA_ARM", "")
    cmd = gpt_astra._official_cmd("do it", tmp_path, "")
    path = codex_loop.base_instructions_file(cmd)
    try:
        assert Path(path).read_text() == gpt_astra._official_system_prompt()
    finally:
        os.unlink(path)


# --- Claude runner (OSW_ARM) ---

def test_the_claude_arm_name_goes_into_the_results_tree(monkeypatch):
    import importlib
    monkeypatch.setenv("OSW_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OSW_EFFORT", "max")
    monkeypatch.setenv("OSW_SYSTEM_SUFFIX", "pkgfix3")
    monkeypatch.setenv("OSW_ARM", "e1card")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.SYSTEM_NAME == "agent_computer_sonnet5_effortmax_pkgfix3_e1card_official"
        reloaded.assert_new_infra_system(reloaded.SYSTEM_NAME)
    finally:
        for k in ("OSW_MODEL", "OSW_EFFORT", "OSW_SYSTEM_SUFFIX", "OSW_ARM"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(config)


def test_the_claude_command_carries_the_card_in_its_system_prompt(monkeypatch):
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "ARM", "e1card")
    kw = agent_computer._official_cmd_kwargs({"instruction": "do it"})
    assert kw["system_prompt"].startswith(agent_computer._official_system_prompt())
    assert kw["system_prompt"].endswith(arms.E1_CARD)
    monkeypatch.setattr(config, "ARM", "")
    assert agent_computer._official_cmd_kwargs({"instruction": "x"})["system_prompt"] == \
        agent_computer._official_system_prompt()


# --- E1 bounce (OSW_ASTRA_ARM=e1bounce) ---

def _ev_msg(i, text):
    return {"type": "item.completed", "item": {"id": f"m{i}", "type": "agent_message", "text": text}}


def _ev_call(i, args):
    return {"type": "item.completed",
            "item": {"id": f"c{i}", "type": "mcp_tool_call", "tool": "computer", "arguments": args}}


def test_the_resume_command_continues_the_session_without_cd(tmp_path):
    from core.codex_loop import build_codex_cmd
    cmd = build_codex_cmd("go on", model="m", cwd=tmp_path, controller_url="",
                          resume_session="sess-1")
    assert cmd[:4] == ["codex", "exec", "resume", "--json"]
    assert "--cd" not in cmd and "--color" not in cmd
    assert cmd[-2:] == ["sess-1", "go on"]
    plain = build_codex_cmd("do it", model="m", cwd=tmp_path, controller_url="")
    assert plain[:5] == ["codex", "exec", "--json", "--color", "never"] and "--cd" in plain


def test_a_bounce_is_needed_only_for_an_unsupported_infeasible_answer(monkeypatch):
    monkeypatch.setattr(config, "ASTRA_ARM", "e1bounce")
    monkeypatch.setattr(config, "MAX_STEPS", 100)
    gave_up = [_ev_call(1, {"action": "screenshot"}), _ev_msg(2, "No settings app. [INFEASIBLE]")]
    probed = [_ev_call(1, {"actions": [{"action": "type", "text": "gsettings list-keys x\n"}]}),
              _ev_msg(2, "[INFEASIBLE] the schema is absent")]
    done = [_ev_call(1, {"action": "screenshot"}), _ev_msg(2, "Done.")]
    assert gpt_astra._bounce_needed(gave_up, 5) is True
    assert gpt_astra._bounce_needed(probed, 5) is False
    assert gpt_astra._bounce_needed(done, 5) is False
    assert gpt_astra._bounce_needed(gave_up, 100) is False   # no steps left
    monkeypatch.setattr(config, "ASTRA_ARM", "e1card")
    assert gpt_astra._bounce_needed(gave_up, 5) is False     # only the bounce arm bounces


def test_the_bounce_command_carries_the_step_budget_and_the_message(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ASTRA_ARM", "e1bounce")
    cmd = gpt_astra._bounce_cmd("sess-9", tmp_path, "http://c", tmp_path, 37)
    try:
        assert cmd[-2:] == ["sess-9", arms.BOUNCE_MESSAGE]
        assert any("OSW_STEPS_USED_START" in a and '"37"' in a for a in cmd)
    finally:
        os.unlink(codex_loop.base_instructions_file(cmd))


def test_the_mcp_server_starts_from_the_carried_step_count(monkeypatch, tmp_path):
    import importlib, json as _json, sys
    state = tmp_path / "state.json"
    monkeypatch.setenv("OSW_MCP_STATE_FILE", str(state))
    monkeypatch.setenv("OSW_STEPS_USED_START", "42")
    sys.modules.pop("benchmarks.osworld.mcp.server", None)
    server = importlib.import_module("benchmarks.osworld.mcp.server")
    try:
        assert server._session.steps_used == 42
        assert _json.loads(state.read_text())["steps_used"] == 42
    finally:
        sys.modules.pop("benchmarks.osworld.mcp.server", None)


# --- E1 bounce, Claude runner (OSW_ARM=e1bounce) ---

def _claude_line(role, content):
    import json as _json
    return _json.dumps({"type": role, "message": {"role": role, "content": content}})


def test_the_claude_bounce_is_needed_only_for_an_unsupported_infeasible(monkeypatch, tmp_path):
    from benchmarks.osworld.runners import agent_computer
    monkeypatch.setattr(config, "ARM", "e1bounce")
    monkeypatch.setattr(config, "MAX_STEPS", 100)
    gave_up = tmp_path / "a.jsonl"
    gave_up.write_text("\n".join([
        _claude_line("assistant", [{"type": "tool_use", "name": "mcp__osworld__computer",
                                    "input": {"action": "screenshot"}}]),
        _claude_line("assistant", [{"type": "text", "text": "No settings app. [INFEASIBLE]"}])]) + "\n")
    probed = tmp_path / "b.jsonl"
    probed.write_text("\n".join([
        _claude_line("assistant", [{"type": "tool_use", "name": "mcp__osworld__computer",
                                    "input": {"action": "type", "text": "which gnome-control-center\n"}}]),
        _claude_line("assistant", [{"type": "text", "text": "[INFEASIBLE]"}])]) + "\n")
    assert agent_computer._bounce_needed(gave_up, 4) is True
    assert agent_computer._bounce_needed(probed, 4) is False
    assert agent_computer._bounce_needed(gave_up, 100) is False
    monkeypatch.setattr(config, "ARM", "e1card")
    assert agent_computer._bounce_needed(gave_up, 4) is False


def test_the_claude_answer_after_a_bounce_ignores_the_rejected_infeasible(tmp_path):
    from benchmarks.osworld.runners import agent_computer
    from benchmarks.osworld import official_protocol
    t = tmp_path / "c.jsonl"
    t.write_text("\n".join([
        _claude_line("assistant", [{"type": "text", "text": "[INFEASIBLE]"}]),
        _claude_line("user", arms.BOUNCE_MESSAGE),
        _claude_line("assistant", [{"type": "tool_use", "name": "mcp__osworld__computer",
                                    "input": {"action": "type", "text": "gsettings set a b c\n"}}]),
        _claude_line("assistant", [{"type": "text", "text": "Done."}])]) + "\n")
    assert official_protocol.final_action(*agent_computer._post_bounce_actions(t)) == "DONE"
