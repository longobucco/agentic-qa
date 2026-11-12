"""The diagnostic manifest and the artefact-capture diagnosis.

Both exist to answer the DEV1 checkpoint's first question -- how many runs are reconstructible,
and why the others are not -- without a person guessing from a directory listing. Measured on the
archive before any DEV run existed: 95.3% of judged runs with an agent (astra 90.4%, sonnet 99.3%).
"""
import json

import pytest

from benchmarks.osworld import diagnostics as D
from benchmarks.osworld.runners import common


def _run(tmp_path, *, tree="agent_computer_gpt6astra_max_codex01534_pkgfix3c_official",
         task="aaaaaaaa-0000-0000-0000-000000000000", name="run_1",
         transcript=True, calls=2, shots=2, final=True, verdict="FAILURE", result=None):
    d = tmp_path / tree / task / name
    d.mkdir(parents=True)
    if transcript:
        lines = []
        for i in range(calls):
            content = [{"type": "text", "text": f"Success[Current step: {i}/100]"}]
            if i < shots:
                content.insert(0, {"type": "image", "data": "iVBORw0KGgo="})
            lines.append(json.dumps({"type": "item.completed", "item": {
                "type": "mcp_tool_call", "tool": "computer",
                "arguments": {"action": "left_click"},
                "result": {"content": content}}}))
        (d / "conversation.jsonl").write_text("\n".join(lines) + "\n")
    if final:
        (d / "final.png").write_bytes(b"\x89PNG")
    (d / "result.json").write_text(json.dumps(result if result is not None else {
        "agent_steps_used": calls, "rollout_archived": True, "rollout_files": ["r.jsonl"],
        "rollout_bytes": 10, "agent_step_counter_resets": 0,
        "phase_seconds": {"provision": 1.0, "setup": 2.0, "agent": 3.0, "bounce": None}}))
    if verdict:
        (d / "eval.json").write_text(json.dumps({"verdict": verdict, "artefact_capture": {
            "reason": "artefacts retrieved", "files_captured": 1}}))
    return d


def test_the_run_key_is_deterministic_and_distinguishes_runs():
    a = D.run_key("tree", "task", "run_1")
    assert a == D.run_key("tree", "task", "run_1")
    assert a != D.run_key("tree", "task", "run_2")
    assert a != D.run_key("other", "task", "run_1")
    assert len(a) == 16


def test_a_complete_run_is_reconstructible(tmp_path):
    rec = D.collect(_run(tmp_path))
    assert rec["reconstructible"] is True and rec["gaps"] == ["no evaluator artefact"]
    t = rec["collected"]["transcript"]
    assert t["present"] and t["tool_calls"] == 2 and t["screenshots"] == 2
    assert rec["collected"]["rollout_archive"]["present"] is True
    assert rec["phase_seconds"]["agent"] == 3.0
    assert rec["step_counter_resets"] == 0


@pytest.mark.parametrize("kwargs,gap", [
    ({"transcript": False}, "no transcript"),
    ({"calls": 0}, "transcript has no tool call"),
    ({"shots": 0}, "transcript has no screenshot"),
    ({"final": False}, "no final screenshot"),
])
def test_every_gap_is_named_rather_than_left_as_a_missing_file(tmp_path, kwargs, gap):
    rec = D.collect(_run(tmp_path, **kwargs))
    assert gap in rec["gaps"]
    if gap != "no final screenshot":
        assert rec["reconstructible"] is False


def test_an_absent_file_carries_a_reason(tmp_path):
    rec = D.collect(_run(tmp_path, final=False))
    assert rec["collected"]["final_screenshot"]["present"] is False
    assert rec["collected"]["final_screenshot"]["reason"]


def test_a_run_without_rollout_archiving_is_not_called_a_claude_run(tmp_path):
    """The first version labelled every archived Astra run 'a runtime without a rollout', which
    is a different fact from 'this predates rollout archiving'."""
    rec = D.collect(_run(tmp_path, result={"agent_steps_used": 1}))
    r = rec["collected"]["rollout_archive"]
    assert r["present"] is False and "before rollout archiving existed" in r["reason"]


def test_a_null_agent_run_is_not_counted_as_a_collection_gap(tmp_path):
    d = _run(tmp_path, tree="agent_computer_nullagent_pkgfix3_official", transcript=False,
             final=False)
    rec = D.collect(d)
    assert rec["has_agent"] is False
    assert rec["reconstructible"] is None and rec["gaps"] == []
    assert "by design" in rec["reconstructible_reason"]


# --- the one thing that cannot be reconstructed afterwards: why there is no artefact (B7) -------

def _task(getter=True):
    ev = {"func": "compare_table", "result": {"type": "vm_file", "path": "/home/user/x.xlsx",
                                              "dest": "x.xlsx"}} if getter else {"func": "check"}
    return {"id": "t", "instruction": "do", "evaluator": ev}


def test_an_empty_manifest_says_why_it_is_empty(tmp_path):
    cache = tmp_path / "gold"
    cache.mkdir()
    rec = common._artefact_capture_diagnosis(cache, _task(), [], None)
    assert rec["files_captured"] == 0 and rec["cache_dir_existed"] is True
    assert "NOT distinguishable" in rec["reason"] and "B7" in rec["reason"]
    assert rec["expected_getters"] and rec["expected_getters"][0]["type"] == "vm_file"


def test_an_evaluator_error_is_distinguished_from_a_silent_empty(tmp_path):
    cache = tmp_path / "gold"
    cache.mkdir()
    rec = common._artefact_capture_diagnosis(cache, _task(), [], "TimeoutError: boom")
    assert "raised before any getter" in rec["reason"] and rec["eval_error"].startswith("Timeout")


def test_a_checker_that_fetches_nothing_is_not_reported_as_a_failure(tmp_path):
    cache = tmp_path / "gold"
    cache.mkdir()
    rec = common._artefact_capture_diagnosis(cache, _task(getter=False), [], None)
    assert "expected" in rec["reason"] and "B7" not in rec["reason"]


def test_a_successful_capture_says_so(tmp_path):
    cache = tmp_path / "gold"
    cache.mkdir()
    (cache / "x.xlsx").write_bytes(b"x")
    rec = common._artefact_capture_diagnosis(cache, _task(), [{"name": "x.xlsx", "kept": True}],
                                             None)
    assert rec["reason"] == "artefacts retrieved" and rec["files_captured"] == 1


def test_the_diagnosis_never_raises_on_a_malformed_task(tmp_path):
    cache = tmp_path / "gold"
    cache.mkdir()
    rec = common._artefact_capture_diagnosis(cache, {"id": "t"}, [], None)
    assert rec["reason"]
