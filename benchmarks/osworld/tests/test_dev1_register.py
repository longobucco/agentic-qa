"""The DEV1 diagnostic tranche register: the properties it is not allowed to lose.

The first draft of this tranche claimed it did not consume the confirmatory reserve, and it did:
`design_input` (228 tasks) and `R3` (216) are overlapping classifications, not a partition, and 63
of the 135 tasks the draft called free were inside R3. These tests exist so that cannot recur
silently.
"""
import json

import pytest

from benchmarks.osworld import dev1_register as D


@pytest.fixture(scope="module")
def reg():
    if not D.MANIFEST.exists():
        pytest.skip("exposure manifest not generated in this checkout")
    return D.build()


def test_the_two_classifications_overlap_and_the_overlap_is_only_arm_doc(reg):
    """The reason the pools are defined against R3 directly rather than against `design_input`."""
    u = reg["eligible_universe"]
    assert u["overlap_design_input_and_r3"] > 0
    assert reg["checks"]["overlap_is_arm_doc_only"] is True
    # and the two counts cannot be a partition of the population
    assert u["design_input"] + u["r3_reserve_before"] > u["population"]


def test_the_tranche_never_overlaps_the_reserve_it_leaves_behind(reg):
    dev = {t["task"] for t in reg["tasks"]}
    reserve = set(reg["reserve_after_ids"])
    assert not dev & reserve
    assert reg["checks"]["dev_disjoint_from_reserve"] is True


def test_every_draw_from_the_reserve_is_declared_and_subtracted(reg):
    drawn = {t["task"] for t in reg["tasks"] if t["pool"] == "r3_declared_draw"}
    assert drawn == set(reg["r3_declared_draw"])
    before = reg["eligible_universe"]["r3_reserve_before"]
    assert reg["reserve_after"]["tasks"] == before - len(drawn)
    assert reg["checks"]["reserve_shrinks_by"] == len(drawn)


def test_the_composition_is_the_approved_one(reg):
    assert sum(D.COMPOSITION.values()) == 40
    assert len(reg["tasks"]) == 40
    assert len({t["task"] for t in reg["tasks"]}) == 40
    from collections import Counter
    assert Counter(t["app"] for t in reg["tasks"]) == Counter(D.COMPOSITION)


def test_no_selected_task_already_has_a_trajectory_on_this_disk(reg):
    """Both pools require it: a diagnostic run on a task whose runs are already readable here
    buys nothing, and the tranche exists to produce reconstructible runs."""
    rows = {r["task"]: r for r in D._rows()}
    for t in reg["tasks"]:
        assert rows[t["task"]]["judged_runs"] == "0", t


def test_selection_is_reproducible_and_rate_blind(reg):
    again = D.build()
    assert again["register_sha256"] == reg["register_sha256"]
    assert reg["selection"]["seed"] == D.SEED
    assert reg["checks"]["selection_used_pass_rates"] is False
    for excluded in ("past pass rate", "past failure count", "difficulty"):
        assert excluded in reg["selection"]["excluded_criteria"]


def test_it_does_not_touch_x22(reg):
    """A separate register: X22 keeps its lock, and this one records its hash to prove which
    version it was frozen against."""
    assert len(reg["x22_lock_sha256"]) == 64
    assert "unchanged" in reg["x22_status"]
    assert reg["run_authorisation"].startswith("NOT granted")


def test_the_second_tranche_cannot_open_just_because_nothing_won(reg):
    s = reg["second_tranche"]
    assert "absence of a winning candidate" in s["never_opens_for"]
    assert s["cap_tasks"] >= 1
    assert reg["checkpoint"]["may_conclude_no_candidate"] is True


def test_the_register_serialises(reg):
    json.dumps(reg)
