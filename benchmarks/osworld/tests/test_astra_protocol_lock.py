"""The Astra runner is official-only: astra_official361_lock.json is the only campaign lock this
branch carries (see test_official_codex_runner.py for the lock-contents/validation coverage --
test_official_lock_validates_once_effort_matches / test_official_lock_refuses_another_effort)."""
from benchmarks.osworld import config


def test_default_lock_is_the_official_campaign():
    assert config.ASTRA_CAMPAIGN_LOCK.name == "astra_official361_lock.json"
