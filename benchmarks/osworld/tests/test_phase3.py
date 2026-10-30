import importlib
import os
from unittest.mock import patch

import pytest

from benchmarks.osworld import config, phase3

CLEAN = {"OSW_MODEL": "", "OSW_EFFORT": "", "OSW_MAX_OUTPUT_TOKENS": "", "OSW_SYSTEM_SUFFIX": "",
         "OSW_ARM": "", "OSW_ASTRA_SYSTEM_SUFFIX": "", "OSW_ASTRA_ARM": "",
         "OSW_ASTRA_REASONING_EFFORT": "", "OSW_ASTRA_EXEMPT_CLOCK": ""}


def _config_for(env):
    with patch.dict(os.environ, {**CLEAN, **env}):
        c = importlib.reload(config)
        out = (c.SYSTEM_NAME, c.ASTRA_SYSTEM_NAME, c.IMAGE)
    importlib.reload(config)
    return out


@pytest.mark.parametrize("arm", sorted(phase3.EXPERIMENTS))
def test_tree_names_match_config(arm):
    set_name = phase3.EXPERIMENTS[arm][0][0]
    for model in phase3.MODELS:
        g = phase3.group(model, arm, set_name)
        sonnet, astra, image = _config_for(g["env"])
        assert g["tree"] == (sonnet if model == "sonnet" else astra)
        assert image == config.SUFFIX_IMAGES["pkgfix3"]


def test_runs_and_exclusions():
    assert phase3.group("sonnet", "a0", "classF")["runs"] == 5
    assert len(phase3.group("sonnet", "a0", "classF")["tasks"]) == 11
    assert len(phase3.group("astra", "a0", "classF")["tasks"]) == 10
    assert len(phase3.group("astra", "e1card", "classF")["tasks"]) == 11
    assert phase3.group("astra", "e1b", "heldoutH")["runs"] == 3


def test_undeclared_groups_are_refused():
    with pytest.raises(SystemExit):
        phase3.group("sonnet", "e1b", "classF")
    with pytest.raises(SystemExit):
        phase3.group("sonnet", "e1typo", "classF")
    with pytest.raises(SystemExit):
        phase3.group("gpt", "a0", "classF")


def test_command_resumes_through_core_run():
    g = phase3.group("astra", "e1card2", "controls")
    cmd = phase3.command(g)
    assert cmd[1:5] == ["-m", "benchmarks.osworld.run", "--system", g["tree"]]
    assert cmd[-2:] == ["--runs", "3"] and len(g["tasks"]) == 20
