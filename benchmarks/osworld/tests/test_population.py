"""The published OSWorld-Verified population: 361 = the 369-task release minus the 8 tasks whose
config needs a real login (login / googledrive steps)."""
import pytest

from benchmarks.osworld import config, tasks
from core.tasks import load_jsonl


@pytest.fixture
def release():
    if not config.TASKS_FILE.exists():
        pytest.skip("task data not downloaded")
    return load_jsonl(config.TASKS_FILE)


def test_login_detection():
    assert tasks.is_login_task({"config": [{"type": "login", "parameters": {}}]})
    assert tasks.is_login_task({"config": [{"type": "googledrive", "parameters": {}}]})
    assert not tasks.is_login_task({"config": [{"type": "launch", "parameters": {}}]})
    assert not tasks.is_login_task({})


def test_release_minus_login_is_361(release):
    assert len(release) == 369
    assert sum(tasks.is_login_task(t) for t in release) == 8


def test_verified361_population(release):
    got = tasks.load_tasks()
    assert len(got) == 361
    assert not any(tasks.is_login_task(t) for t in got)
