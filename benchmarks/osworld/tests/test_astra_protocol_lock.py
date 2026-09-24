"""The Astra runner is official-only: astra_official361_lock.json is the only campaign lock this
branch carries (see test_official_codex_runner.py for the fuller lock-contents/validation
coverage)."""
from pathlib import Path

import pytest

from benchmarks.osworld import config

ROOT = Path(__file__).resolve().parents[3]


def test_default_lock_is_the_official_campaign():
    assert config.ASTRA_CAMPAIGN_LOCK.name == "astra_official361_lock.json"


def test_lock_matches_the_configured_effort(monkeypatch):
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld/astra_official361_lock.json")
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "max")
    gpt_astra._validate_campaign_lock()


def test_undecided_effort_refuses_the_campaign(monkeypatch):
    from benchmarks.osworld.runners import gpt_astra
    monkeypatch.setattr(gpt_astra, "_LOCK", ROOT / "benchmarks/osworld/astra_official361_lock.json")
    monkeypatch.setattr(config, "ASTRA_REASONING_EFFORT", "high")
    with pytest.raises(SystemExit):
        gpt_astra._validate_campaign_lock()
