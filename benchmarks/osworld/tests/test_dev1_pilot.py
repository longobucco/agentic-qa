"""The DEV1 pilot gate, exercised before any DEV run exists.

Every condition is a halt: on a failure the tranche stops and the runs already produced are kept.
These tests build synthetic run directories so the bar is proven to work while it can still be
argued about -- not after results are visible.
"""
import json

import pytest

from benchmarks.osworld import config, dev1_pilot


@pytest.fixture
def lock():
    return json.loads(dev1_pilot.LOCK.read_text())


@pytest.fixture
def register():
    if not dev1_pilot.REGISTER.exists():
        pytest.skip("DEV1 register not frozen in this checkout")
    return json.loads(dev1_pilot.REGISTER.read_text())


def _run(tmp_path, lock, task, *, tree="agent_computer_gpt6astra_pkgfix3cdev1_official", **over):
    d = tmp_path / tree / task / "run_1"
    d.mkdir(parents=True)
    lines = [json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "tool": "computer", "arguments": {"action": "left_click"},
        "result": {"content": [{"type": "image", "data": "iVBOR"},
                               {"type": "text", "text": "Success[Current step: 1/100]"}]}}})]
    (d / "conversation.jsonl").write_text("\n".join(lines) + "\n")
    (d / "final.png").write_bytes(b"\x89PNG")
    result = {"rollout_archived": True, "rollout_files": ["r.jsonl"], "rollout_bytes": 9,
              "agent_step_counter_resets": 0, "agent_steps_used": 1,
              "phase_seconds": {"provision": 30.0, "setup": 5.0, "agent": 60.0, "bounce": None},
              "provenance": {"image": lock["image"], "observation": "screenshot", "arm": None,
                             "harness_dirty": False}}
    for k, v in over.items():
        if k in ("provenance",):
            result["provenance"].update(v)
        else:
            result[k] = v
    (d / "result.json").write_text(json.dumps(result))
    (d / "eval.json").write_text(json.dumps({
        "verdict": "FAILURE", "artefact_capture": {"reason": "artefacts retrieved"}}))
    return d


def test_a_well_collected_run_clears_the_gate(tmp_path, lock):
    d = _run(tmp_path, lock, "t1")
    bad, rec = dev1_pilot.check_run(d, lock, "agent_computer_gpt6astra_pkgfix3cdev1_official")
    assert bad == [], bad
    assert rec["reconstructible"] is True and rec["run_key"]


@pytest.mark.parametrize("over,needle", [
    ({"rollout_archived": False}, "rollout not archived"),
    ({"agent_step_counter_resets": 1}, "restarted mid-run"),
    ({"phase_seconds": {"provision": None, "setup": 1.0, "agent": 1.0}}, "phase timing missing"),
    ({"provenance": {"image": "ghcr.io/x@sha256:dead"}}, "not the locked guest"),
    ({"provenance": {"observation": "screenshot+command_output"}}, "screenshots only"),
    ({"provenance": {"arm": "e1card2"}}, "runs a0"),
    ({"provenance": {"harness_dirty": True}}, "harness_dirty"),
])
def test_each_condition_halts(tmp_path, lock, over, needle):
    d = _run(tmp_path, lock, "t1", **over)
    bad, _ = dev1_pilot.check_run(d, lock, "agent_computer_gpt6astra_pkgfix3cdev1_official")
    assert any(needle in b for b in bad), bad


def test_an_undiagnosed_empty_artefact_manifest_halts(tmp_path, lock):
    """B7: an empty manifest with no recorded reason is exactly the state that made 51 failures
    undiagnosable. A DEV run is not allowed to reproduce it."""
    d = _run(tmp_path, lock, "t1")
    (d / "eval.json").write_text(json.dumps({"verdict": "FAILURE"}))
    bad, _ = dev1_pilot.check_run(d, lock, "agent_computer_gpt6astra_pkgfix3cdev1_official")
    assert any("B7" in b for b in bad), bad


def test_the_pilot_exercises_the_riskiest_environments_first(register):
    """A pilot that only ran well-known applications would prove nothing about the ones the
    tranche exists to see: vscode has never been run, thunderbird has one judged task of 23."""
    picked = dev1_pilot.pilot_tasks(register)
    assert len(picked) == dev1_pilot.PILOT_RUNS
    assert [p["app"] for p in picked] == list(dev1_pilot.PILOT_APP_ORDER[:dev1_pilot.PILOT_RUNS])
    assert picked[0]["app"] == "vscode"
    assert len({p["task"] for p in picked}) == dev1_pilot.PILOT_RUNS
    assert all(p["task"] in {t["task"] for t in register["tasks"]} for p in picked)
    assert dev1_pilot.pilot_tasks(register) == picked                 # deterministic


def test_the_gate_refuses_a_tree_without_the_dev_suffix(tmp_path, register, monkeypatch):
    monkeypatch.setattr(config, "ASTRA_SYSTEM_NAME",
                        "agent_computer_gpt6astra_max_codex01534_pkgfix3c_official")
    rep = dev1_pilot.scan(tmp_path, register=register)
    assert any("does not carry the DEV1 suffix" in v["reason"] for v in rep["violations"])
    assert rep["clear"] is False


def test_a_task_outside_the_register_is_a_violation(tmp_path, lock, register, monkeypatch):
    tree = "agent_computer_gpt6astra_max_codex01534_pkgfix3cdev1_official"
    monkeypatch.setattr(config, "ASTRA_SYSTEM_NAME", tree)
    _run(tmp_path, lock, register["tasks"][0]["task"], tree=tree)
    _run(tmp_path, lock, "ffffffff-0000-0000-0000-000000000000", tree=tree)
    rep = dev1_pilot.scan(tmp_path, register=register)
    assert any("outside the register" in v["reason"] for v in rep["violations"])


def test_a_clean_pilot_clears_and_counts_its_runs(tmp_path, lock, register, monkeypatch):
    tree = "agent_computer_gpt6astra_max_codex01534_pkgfix3cdev1_official"
    monkeypatch.setattr(config, "ASTRA_SYSTEM_NAME", tree)
    for t in dev1_pilot.pilot_tasks(register):
        _run(tmp_path, lock, t["task"], tree=tree)
    rep = dev1_pilot.scan(tmp_path, register=register)
    assert rep["violations"] == [] and rep["clear"] is True
    assert rep["checked"] == dev1_pilot.PILOT_RUNS
    assert all(r["clear"] for r in rep["pilot_runs"])
