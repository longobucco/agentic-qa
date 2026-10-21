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
