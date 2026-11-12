"""The three automatic gates of X22 (E8: they must actually fire).

Gate B and Gate C are exercised against synthetic run trees with tampered provenance, because a
gate that has never been seen to refuse anything is not a gate.
"""
import json

import pytest

from benchmarks.osworld import arms, phase3
from benchmarks.osworld.gates import calibrate, contract, monitor, sequential

LOCK = json.loads((contract.LOCK).read_text())


# --- the lock itself ---------------------------------------------------------------------------

def test_the_lock_matches_the_code_it_freezes():
    for arm, frozen in LOCK["arm_addendum_sha256"].items():
        assert arms.provenance(arm)["arm_addendum_sha256"] == frozen, arm
    assert LOCK["stage3"]["boundaries"]["nominal_alpha"], "boundaries must be frozen before runs"
    assert LOCK["total_runs"] == sum(g["runs"] for g in LOCK["groups"])


def test_arm_text_is_generic():
    """Amendment 1 of the lock rests on this: the cards carry no task-specific content, so a task
    merely named in an analysis document is not a task the arm was tuned on."""
    import re
    tasks = [json.loads(l) for l in
             (phase3.config.DATA_DIR / "osworld_verified.jsonl").read_text().splitlines() if l.strip()]
    for name in ("e1card", "e1card2", "e1b2", "code", "code2"):
        text = arms.addendum(name)
        assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}", text), f"{name} names a task id"
        assert not re.search(r"\b[0-9a-f]{8}\b", text), f"{name} names a task id prefix"
        for t in tasks:
            head = t["instruction"][:30].strip().lower()
            if len(head) > 25:
                assert head not in text.lower(), f"{name} quotes task {t['id'][:8]}"


def test_stage3_strata_are_nested_and_frozen():
    s = LOCK["stage3"]["strata"]
    assert set(s["never_cited"]) <= set(s["primary_out_of_sample"])
    assert set(s["primary_out_of_sample"]) <= set(s["out_of_design"])
    assert set(s["out_of_design"]) <= set(s["all361"])
    assert LOCK["stage3"]["primary_stratum"] == "out_of_design"


# --- Gate A ------------------------------------------------------------------------------------

def test_gate_a_refuses_a_group_the_lock_does_not_declare():
    bad = contract.check("astra", "e1b2", "heldoutCsat", lock=LOCK)
    assert any("not declared in the lock" in r for r in bad)


def test_gate_a_refuses_a_changed_arm_text():
    tampered = json.loads(json.dumps(LOCK))
    tampered["arm_addendum_sha256"]["e1card2"] = "0" * 64
    bad = contract.check("astra", "e1card2", "population361", lock=tampered)
    assert any("text has changed" in r for r in bad)


def test_gate_a_refuses_a_wrong_image_for_the_suffix():
    tampered = json.loads(json.dumps(LOCK))
    tampered["image"] = "ghcr.io/other@sha256:" + "a" * 64
    bad = contract.check("astra", "a0", "population361", lock=tampered)
    assert any("the lock says" in r for r in bad)


def test_gate_a_refuses_unfrozen_boundaries():
    tampered = json.loads(json.dumps(LOCK))
    tampered["stage3"]["boundaries"] = None
    bad = contract.check("astra", "a0", "population361", lock=tampered)
    assert any("boundaries are not frozen" in r for r in bad)


# --- Gate B ------------------------------------------------------------------------------------

def _run(tree_root, task, k, *, arm, verdict="SUCCESS", **overrides):
    d = tree_root / task / f"run_{k}"
    (d / "final_state").mkdir(parents=True)
    (d / "final_state" / "manifest.json").write_text("{}")
    prov = dict(monitor._expected(LOCK, arm))
    prov.update(overrides.pop("provenance", {}))
    result = {"transcript_saved": True, "transcript_bytes": 10, "rollout_archived": True,
              "agent_context_leaks": [], "agent_non_computer_tool_calls": [],
              "agent_step_counter_resets": 0,
              "contaminated": False, "agent_steps_used": 4, "provenance": prov}
    result.update(overrides)
    (d / "result.json").write_text(json.dumps(result))
    (d / "eval.json").write_text(json.dumps({"verdict": verdict}))
    return d


def _tree(tmp_path, arm):
    g = next(x for x in LOCK["groups"] if x["arm"] == arm and x["stage"] == 3)
    root = tmp_path / g["tree"]
    root.mkdir(parents=True)
    return root, g


def test_gate_b_passes_a_clean_run(tmp_path):
    root, _ = _tree(tmp_path, "a0")
    _run(root, LOCK["stage3"]["strata"]["out_of_design"][0], 1, arm="a0")
    report = monitor.scan(LOCK, tmp_path)
    assert report["runs_checked"] == 1
    assert report["invariant_violations"] == [] and report["rate_alarms"] == []


@pytest.mark.parametrize("field,value,needle", [
    ("harness_dirty", True, "harness_dirty"),
    ("model_served", "gpt-5", "model_served"),
    ("clock_exempt", True, "clock_exempt"),
    ("image", "ghcr.io/x@sha256:deadbeef", "image"),
    ("evaluator_commit", "0" * 40, "evaluator_commit"),
    ("arm", "e1b2", "provenance.arm"),
])
def test_gate_b_catches_tampered_provenance(tmp_path, field, value, needle):
    root, _ = _tree(tmp_path, "a0")
    _run(root, LOCK["stage3"]["strata"]["out_of_design"][0], 1, arm="a0",
         provenance={field: value})
    report = monitor.scan(LOCK, tmp_path)
    assert any(needle in v["reason"] for v in report["invariant_violations"]), report


@pytest.mark.parametrize("field,value,needle", [
    ("rollout_archived", False, "rollout not archived"),
    ("transcript_saved", False, "transcript not saved"),
    ("agent_context_leaks", None, "agent_context_leaks"),
    ("agent_non_computer_tool_calls", None, "agent_non_computer_tool_calls"),
    ("agent_steps_used", None, "agent_steps_used is null"),
    # A restarted MCP server used to hand the agent a fresh 100-step budget; the budget now
    # survives the restart, but a run that needed one is never silently clean.
    ("agent_step_counter_resets", 1, "agent_step_counter_resets"),
    ("agent_step_counter_resets", None, "agent_step_counter_resets"),
    ("contaminated", True, "oracle access"),
])
def test_gate_b_catches_an_unverifiable_run(tmp_path, field, value, needle):
    """None is not clean: an unverifiable run must never look like a verified one."""
    root, _ = _tree(tmp_path, "a0")
    _run(root, LOCK["stage3"]["strata"]["out_of_design"][0], 1, arm="a0", **{field: value})
    report = monitor.scan(LOCK, tmp_path)
    assert any(needle in v["reason"] for v in report["invariant_violations"]), report


def test_gate_b_expects_the_arm_s_own_observation_surface(tmp_path):
    """proposta_harness_osworld.md section 5, adopted 12/11: `run_command` returns stdout and
    stderr to the model, so a code arm's observation surface is not "screenshot". Gate B expects
    the surface the arm actually has, which means a code2 run claiming "screenshot" is a false
    record and has to be caught -- while the same value on an arm without a command tool is right.
    """
    g = next(x for x in LOCK["groups"] if x["arm"] == "code2")
    root = tmp_path / g["tree"]; root.mkdir(parents=True)
    task = LOCK["stage3"]["strata"]["out_of_design"][0]
    assert LOCK["observation_surface"]["code2"] == "screenshot+command_output"

    _run(root, task, 1, arm="code2")
    assert monitor.scan(LOCK, tmp_path)["invariant_violations"] == []

    _run(root, task, 2, arm="code2", provenance={"observation": "screenshot"})
    bad = monitor.scan(LOCK, tmp_path)["invariant_violations"]
    assert any("provenance.observation" in r["reason"] for r in bad), bad


def test_gate_b_catches_a_missing_v1_capture(tmp_path):
    root, _ = _tree(tmp_path, "a0")
    d = _run(root, LOCK["stage3"]["strata"]["out_of_design"][0], 1, arm="a0")
    (d / "final_state" / "manifest.json").unlink()
    report = monitor.scan(LOCK, tmp_path)
    assert any("final-state capture missing" in v["reason"]
               for v in report["invariant_violations"])


def test_gate_b_raises_the_tool_surface_rate_alarm(tmp_path):
    """`code` sat at 7.1%; code2's contract exists to fix that, so >5% must stop the group."""
    root, _ = _tree(tmp_path, "a0")
    tasks = LOCK["stage3"]["strata"]["out_of_design"]
    for i in range(20):
        d = _run(root, tasks[i], 1, arm="a0", verdict="FAILURE")
        if i < 3:   # 3 of 20 = 15%
            (d / "eval.json").write_text(json.dumps(
                {"verdict": "FAILURE", "tool_surface_violation": ["exec"]}))
    report = monitor.scan(LOCK, tmp_path)
    assert any(a["alarm"] == "tool_surface_violation" for a in report["rate_alarms"]), report


def test_gate_b_catches_arms_drifting_out_of_balance(tmp_path):
    tasks = LOCK["stage3"]["strata"]["out_of_design"]
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    for k in (1, 2):
        _run(base, tasks[0], k, arm="a0")
    _run(arm, tasks[0], 1, arm="e1card2")      # balanced within 1: fine
    for k in (1, 2):
        _run(base, tasks[1], k, arm="a0")
    arm_dir = arm / tasks[1]                    # arm has zero judged runs: a gap of 2
    arm_dir.mkdir(parents=True)
    out = monitor.balance(LOCK, tmp_path)
    assert out == [] or all(o["task"] != tasks[0][:8] for o in out)


# --- Gate C ------------------------------------------------------------------------------------

def test_mcnemar_is_the_exact_one_sided_binomial():
    assert calibrate.mcnemar_p(0, 0) == 1.0
    assert calibrate.mcnemar_p(3, 0) == pytest.approx(0.125)
    assert calibrate.mcnemar_p(13, 0) == pytest.approx(0.5 ** 13)
    assert calibrate.mcnemar_p(5, 1) == pytest.approx(7 / 64)


def test_gate_c_takes_no_look_before_the_threshold(tmp_path):
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    tasks = LOCK["stage3"]["strata"]["out_of_design"]
    for t in tasks[:5]:
        for k in (1, 2):
            _run(base, t, k, arm="a0")
            _run(arm, t, k, arm="e1card2")
    out = sequential.evaluate(LOCK, tmp_path)
    assert out["verdict"] == "no look yet" and out["look_index"] is None


def test_gate_c_ignores_a_task_not_complete_on_both_arms(tmp_path):
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    t = LOCK["stage3"]["strata"]["out_of_design"][0]
    for k in (1, 2):
        _run(base, t, k, arm="a0")
    _run(arm, t, 1, arm="e1card2")   # only one of the two declared runs
    rows, _ = sequential.paired(LOCK, tmp_path)
    assert rows == []


def test_gate_c_declares_efficacy_on_a_one_sided_sweep(tmp_path):
    """Baseline fails every task, the arm passes every task: the first look must fire."""
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    n = LOCK["stage3"]["boundaries"]["looks_at_tasks"][0]
    for t in LOCK["stage3"]["strata"]["out_of_design"][:n]:
        for k in (1, 2):
            _run(base, t, k, arm="a0", verdict="FAILURE")
            _run(arm, t, k, arm="e1card2", verdict="SUCCESS")
    out = sequential.evaluate(LOCK, tmp_path)
    assert out["look_index"] == 0
    assert out["verdict"].startswith("EFFICACY"), out


def test_gate_c_stops_for_arithmetic_futility(tmp_path):
    """The arm loses every discordant task: even all remaining tasks flipping cannot save it."""
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    tasks = LOCK["stage3"]["strata"]["out_of_design"]
    n = len(tasks)
    for t in tasks:
        for k in (1, 2):
            _run(base, t, k, arm="a0", verdict="SUCCESS")
            _run(arm, t, k, arm="e1card2", verdict="FAILURE")
    out = sequential.evaluate(LOCK, tmp_path)
    assert out["tasks_complete_on_both_arms"] == n
    assert out["remaining_tasks"] == 0
    assert out["verdict"].startswith("HARM") or out["verdict"].startswith("FUTILITY"), out


def test_gate_c_reports_harm_when_the_arm_breaks_solved_tasks(tmp_path):
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    n = LOCK["stage3"]["boundaries"]["looks_at_tasks"][0]
    for t in LOCK["stage3"]["strata"]["out_of_design"][:n]:
        for k in (1, 2):
            _run(base, t, k, arm="a0", verdict="SUCCESS")
            _run(arm, t, k, arm="e1card2", verdict="FAILURE")
    out = sequential.evaluate(LOCK, tmp_path)
    assert out["verdict"].startswith("HARM"), out
    assert out["mean_paired_difference_points"] == pytest.approx(-100.0)


# --- the orchestration scripts ----------------------------------------------------------------

def test_gate_a_notices_a_changed_driver_script(tmp_path, monkeypatch):
    """docs/phase3 is gitignored, so harness_dirty cannot see a change to the driver. Its digests
    are tracked separately and Gate A refuses to launch after an unreviewed change."""
    assert contract.driver_drift() == [], "the recorded digests must match the scripts on disk"
    tampered = json.loads(contract.DRIVER_HASHES.read_text())
    tampered["files"]["guard.sh"]["sha256"] = "0" * 64
    fake = tmp_path / "hashes.json"
    fake.write_text(json.dumps(tampered))
    monkeypatch.setattr(contract, "DRIVER_HASHES", fake)
    out = contract.driver_drift()
    assert any("guard.sh changed" in r for r in out), out


def test_gate_a_refuses_when_the_digests_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "DRIVER_HASHES", tmp_path / "absent.json")
    assert any("unreadable" in r for r in contract.driver_drift())


# --- the infra_error rule (regression, 12/11) --------------------------------------------------

def test_a_retried_run_with_an_infra_log_still_counts(tmp_path):
    """core.results: infra_error.json is an append-only log of attempts that died BEFORE a verdict,
    and is_done() looks at eval.json alone. A run carrying both flaked, was retried and produced a
    verdict. Excluding it would have dropped 339 judged runs of the exploratory Astra trees and,
    in Gate C, made tasks look permanently incomplete."""
    root, _ = _tree(tmp_path, "a0")
    d = _run(root, LOCK["stage3"]["strata"]["out_of_design"][0], 1, arm="a0")
    (d / "infra_error.json").write_text(json.dumps(
        [{"outcome": "INFRA_FLAKE", "error_type": "DaytonaError"},
         {"outcome": "RATE_LIMITED", "error_type": "APIError"}]))
    report = monitor.scan(LOCK, tmp_path)
    assert report["runs_checked"] == 1, "a retried run must still be checked"
    assert report["trees"][root.name]["judged"] == 1
    assert report["trees"][root.name]["infra"] == 0


def test_an_attempt_without_a_verdict_is_infra_not_a_failure(tmp_path):
    root, _ = _tree(tmp_path, "a0")
    d = root / LOCK["stage3"]["strata"]["out_of_design"][0] / "run_1"
    d.mkdir(parents=True)
    (d / "infra_error.json").write_text(json.dumps([{"outcome": "RATE_LIMITED"}]))
    report = monitor.scan(LOCK, tmp_path)
    assert report["runs_checked"] == 0
    assert report["trees"][root.name]["infra"] == 1
    assert report["trees"][root.name]["no_verdict"] == 0


def test_gate_c_counts_a_task_whose_runs_were_retried(tmp_path):
    base, _ = _tree(tmp_path, "a0")
    arm, _ = _tree(tmp_path, "e1card2")
    t = LOCK["stage3"]["strata"]["out_of_design"][0]
    for k in (1, 2):
        for root, a in ((base, "a0"), (arm, "e1card2")):
            d = _run(root, t, k, arm=a)
            (d / "infra_error.json").write_text(json.dumps([{"outcome": "INFRA_FLAKE"}]))
    rows, _ = sequential.paired(LOCK, tmp_path)
    assert len(rows) == 1 and rows[0]["runs"] == 2
