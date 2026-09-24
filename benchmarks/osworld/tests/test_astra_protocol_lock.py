import importlib
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


def test_undecided_effort_refuses_the_campaign(monkeypatch):
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld/astra_protocol361_lock.json")
    with pytest.raises(SystemExit):
        gpt_astra._validate_campaign_lock()
