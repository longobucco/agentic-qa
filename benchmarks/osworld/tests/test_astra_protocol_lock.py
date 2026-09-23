import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.osworld import config

ROOT = Path(__file__).resolve().parents[3]


def test_default_lock_is_the_frozen_campaign():
    assert config.ASTRA_CAMPAIGN_LOCK.name == "astra_campaign_lock.json"


def test_lock_is_selectable():
    with patch.dict(os.environ, {"OSW_ASTRA_CAMPAIGN_LOCK": "astra_protocol361_lock.json"}):
        c = importlib.reload(config)
        name = c.ASTRA_CAMPAIGN_LOCK.name
    importlib.reload(config)
    assert name == "astra_protocol361_lock.json"


@pytest.mark.parametrize("lock,extra", [("astra_protocol361_lock.json", ()),
                                        ("astra_protocol361_zoombatch_lock.json", ("zoom", "batch"))])
def test_protocol_locks_are_consistent(lock, extra):
    from core.codex_loop import ALLOWED_MCP_TOOLS
    data = json.loads((ROOT / "benchmarks/osworld" / lock).read_text())
    assert data["population"]["count"] == 361
    assert tuple(data["tool_policy"]["allowed_mcp_tools"]) == ALLOWED_MCP_TOOLS + extra


def test_undecided_effort_refuses_the_campaign(monkeypatch):
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld/astra_protocol361_lock.json")
    with pytest.raises(SystemExit):
        gpt_astra._validate_campaign_lock()


@pytest.mark.parametrize("lock,zoom_batch", [("astra_protocol361_lock.json", False),
                                             ("astra_protocol361_zoombatch_lock.json", True)])
def test_protocol_lock_validates_once_effort_matches(monkeypatch, lock, zoom_batch):
    """Population hash and tool policy of each protocol lock pass the real preflight check."""
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld" / lock)
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "REQUIRES_USER_DECISION")
    monkeypatch.setattr(config, "ZOOM_BATCH", zoom_batch)
    gpt_astra._validate_campaign_lock()
