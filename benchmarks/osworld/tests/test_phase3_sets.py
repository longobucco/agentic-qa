import json

from benchmarks.osworld import config

SETS = json.loads((config.DATA_DIR / "phase3_sets.json").read_text())["sets"]


def test_sets_are_known_tasks_without_overlap():
    ids = {json.loads(line)["id"] for line in config.TASKS_FILE.read_text().splitlines() if line}
    all_tasks = [t for s in SETS.values() for t in s["tasks"]]
    assert set(all_tasks) <= ids
    assert len(all_tasks) == len(set(all_tasks))


def test_every_set_states_its_rule_date_and_runs():
    for name, s in SETS.items():
        assert s["rule"] and s["fixed_on"] and s["runs_per_task"] in (3, 5), name


def test_class_f_carries_the_eleventh_task():
    assert len(SETS["classF"]["tasks"]) == 11
    assert "3ce045a0-877b-42aa-8d2c-b4a863336ab8" in SETS["classF"]["tasks"]
