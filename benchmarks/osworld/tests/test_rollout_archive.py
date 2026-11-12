"""The run's own copy of Codex's rollout (astra_common.archive_rollouts).

Why this exists: the saved `conversation.jsonl` is not the trajectory -- the `codex exec --json`
stream carries only mcp_tool_call items and the final agent_message, so the exec scripts the model
wrote live only in the rollout under CODEX_HOME, outside the results tree.
"""
import json

from benchmarks.osworld.runners import astra_common


def _rollout(root, session_id, name=None, body=b'{"type":"session_meta"}\n'):
    day = root / "2026" / "11" / "12"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or f"rollout-2026-11-12T10-00-00-{session_id}.jsonl")
    path.write_bytes(body)
    return path


def test_archives_the_rollout_with_its_size_and_digest(tmp_path):
    sessions, out = tmp_path / "sessions", tmp_path / "run_1"
    out.mkdir()
    src = _rollout(sessions, "sess-a", body=b'{"type":"response_item"}\n' * 10)
    rec = astra_common.archive_rollouts(out, "sess-a", root=sessions)
    copied = out / astra_common.ROLLOUT_DIR / src.name
    assert rec["rollout_archived"] is True
    assert copied.read_bytes() == src.read_bytes()
    assert rec["rollout_bytes"] == src.stat().st_size
    assert [f["name"] for f in rec["rollout_files"]] == [src.name]
    assert len(rec["rollout_files"][0]["sha256"]) == 64
    assert rec["rollout_error"] is None


def test_archives_every_file_of_a_resumed_session(tmp_path):
    """An arm's bounce continues the same session: Codex may write more than one rollout for it,
    and keeping only the latest would lose the part before the bounce."""
    sessions, out = tmp_path / "sessions", tmp_path / "run_1"
    out.mkdir()
    a = _rollout(sessions, "sess-b", name="rollout-2026-11-12T10-00-00-sess-b.jsonl", body=b"a\n")
    b = _rollout(sessions, "sess-b", name="rollout-2026-11-12T11-00-00-sess-b.jsonl", body=b"bb\n")
    rec = astra_common.archive_rollouts(out, "sess-b", root=sessions)
    assert {f["name"] for f in rec["rollout_files"]} == {a.name, b.name}
    assert rec["rollout_bytes"] == a.stat().st_size + b.stat().st_size


def test_another_session_is_never_copied(tmp_path):
    sessions, out = tmp_path / "sessions", tmp_path / "run_1"
    out.mkdir()
    _rollout(sessions, "sess-other")
    rec = astra_common.archive_rollouts(out, "sess-mine", root=sessions)
    assert rec["rollout_archived"] is False
    assert rec["rollout_files"] == [] and rec["rollout_bytes"] == 0
    assert "no rollout" in rec["rollout_error"]


def test_a_missing_session_id_is_recorded_not_raised(tmp_path):
    rec = astra_common.archive_rollouts(tmp_path, None, root=tmp_path / "sessions")
    assert rec["rollout_archived"] is False and rec["rollout_error"] == "no session id"


def test_the_record_is_json_serialisable_for_result_json(tmp_path):
    sessions, out = tmp_path / "sessions", tmp_path / "run_1"
    out.mkdir()
    _rollout(sessions, "sess-c")
    rec = astra_common.archive_rollouts(out, "sess-c", root=sessions)
    assert json.loads(json.dumps(rec))["rollout_archived"] is True
