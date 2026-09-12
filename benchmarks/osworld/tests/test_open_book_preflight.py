"""Pure regression tests for open_book_preflight.py and the fixture bundle format it depends on
(openbook_proxy/bundle.py) -- no live sandbox, no mitmproxy."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from benchmarks.osworld import open_book_preflight as p
from benchmarks.osworld.openbook_proxy import bundle as bundlemod
from scripts.build_astra_openbook_manifest import build_manifest_records, proxy_tags


def _lock(**over):
    base = json.loads(p._LOCK.read_text())
    base.update(over)
    return base


def test_a_draft_lock_refuses_the_campaign_check():
    """No canary, no authored fixtures yet -- campaign_check must refuse to run anything for
    real against the committed draft lock (see its own _status_note)."""
    lock = p.load_lock()
    assert lock["status"] == "draft_pending_canary"
    try:
        p._validate_lock(lock)
        assert False, "a draft-status lock must raise"
    except SystemExit as e:
        assert "not 'frozen'" in str(e)


def test_a_frozen_lock_with_a_tampered_population_hash_is_rejected():
    lock = _lock(status="frozen")
    lock["population"]["sha256"] = "0" * 64
    try:
        p._validate_lock(lock)
        assert False, "a tampered population hash must raise"
    except SystemExit as e:
        assert "population hash mismatch" in str(e)


def test_a_frozen_lock_with_a_wrong_model_is_rejected():
    lock = _lock(status="frozen", model="gpt-5-mini")
    try:
        p._validate_lock(lock)
        assert False, "a model mismatch must raise"
    except SystemExit as e:
        assert "mismatch for model" in str(e)


def test_a_frozen_lock_with_a_tampered_tool_policy_is_rejected():
    lock = _lock(status="frozen")
    lock["tool_policy"]["allowed_mcp_tools"] = ["screenshot"]
    try:
        p._validate_lock(lock)
        assert False, "a narrower tool policy than the frozen one must still be rejected"
    except SystemExit as e:
        assert "tool policy" in str(e)


def test_require_open_book_rejects_a_task_outside_the_population():
    lock = p.load_lock()
    try:
        p.require_open_book({"id": "not-a-real-id", "instruction": "", "related_apps": []},
                            lock=lock)
        assert False
    except p.PreflightError as e:
        assert "not in the frozen open-book population" in str(e)


def test_require_open_book_rejects_a_task_that_no_longer_classifies_as_open_book():
    lock = p.load_lock()
    task_id = next(iter(p._population_ids(lock)))
    with patch("benchmarks.osworld.closed_book.classify",
              return_value={"task_id": task_id, "app": "chrome", "book": "closed-book",
                            "reasons": []}):
        try:
            p.require_open_book({"id": task_id, "instruction": "", "related_apps": []},
                                lock=lock)
            assert False
        except p.PreflightError as e:
            assert "now classifies as" in str(e)


def _real_open_book_task(lock):
    """A fabricated near-empty task record reclassifies as closed-book (no URL, no download
    step) and gets rejected one check earlier than intended -- load the real task so
    require_open_book's reclassification agrees with why the id is in the population."""
    from benchmarks.osworld.tasks import load_tasks
    by_id = {t["id"]: t for t in load_tasks()}
    task_id = next(iter(p._population_ids(lock)))
    return by_id[task_id]


def test_task_check_reports_snapshot_proxy_missing_for_an_unauthored_bundle():
    lock = p.load_lock()
    task = _real_open_book_task(lock)
    result = p.task_check(task, lock=lock)
    assert result["ready"] is False
    assert "SNAPSHOT_PROXY_MISSING" in result["reason"]


def test_task_check_succeeds_against_a_real_authored_bundle():
    lock = p.load_lock()
    task = _real_open_book_task(lock)
    task_id = task["id"]
    tmp_fixtures = Path(tempfile.mkdtemp())
    bundle_dir = tmp_fixtures / task_id
    manifest = bundlemod.build_manifest(
        task_id, http=[{"method": "GET", "url": "https://example.com/x", "body": b"hi"}])
    bundlemod.write_bundle(bundle_dir, manifest,
                          {e["body_file"]: b"hi" for e in manifest["http"]})
    with patch.object(p, "_FIXTURES_DIR", tmp_fixtures):
        result = p.task_check(task, lock=lock)
    assert result["ready"] is True
    assert result["manifest_sha256"]


def test_manifest_generator_includes_every_and_only_open_book_tasks():
    fake_tasks = [
        {"id": "closed-1", "instruction": "no url here", "related_apps": ["libreoffice_calc"],
         "config": [], "evaluator": {}},
        {"id": "open-1", "instruction": "visit https://example.com", "related_apps": ["chrome"],
         "config": [], "evaluator": {}},
    ]
    records = build_manifest_records(fake_tasks)
    ids = {r["task_id"] for r in records}
    assert ids == {"open-1"}


def test_proxy_tags_distinguish_host_side_from_cdp_driven_getters():
    cloud_task = {"id": "x", "evaluator": {"result": {"type": "cloud_file"}}}
    drive_task = {"id": "y", "evaluator": {"expected": {"type": "googledrive_file"}}}
    cdp_task = {"id": "z", "evaluator": {"result": {"type": "info_from_website"}}}
    plain_task = {"id": "w", "evaluator": {"result": {"type": "vm_file"}}}
    assert proxy_tags(cloud_task) == ["external_live_getter"]
    assert proxy_tags(drive_task) == ["external_oauth_service"]
    assert proxy_tags(cdp_task) == ["cdp_driven_live_getter"]
    assert proxy_tags(plain_task) == []


def test_fixture_bundle_round_trips_http_and_drive_entries():
    manifest = bundlemod.build_manifest(
        "task-x",
        http=[{"method": "GET", "url": "https://example.com/page?b=2&a=1", "status": 200,
              "body": b"<html>hi</html>"}],
        drive=[{"path": ["folder", "file.txt"], "body": b"hello drive"}],
    )
    bodies = {e["body_file"]: b"<html>hi</html>" for e in manifest["http"]}
    bodies.update({e["body_file"]: b"hello drive" for e in manifest["drive"]})
    tmp = Path(tempfile.mkdtemp())
    bundlemod.write_bundle(tmp, manifest, bodies)
    loaded = bundlemod.FixtureBundle.load(tmp)
    hit = loaded.lookup_http("GET", "https://example.com/page?a=1&b=2")
    assert hit.status == 200 and hit.body == b"<html>hi</html>"
    assert loaded.lookup_http("GET", "https://example.com/missing") is None
    assert loaded.misses and loaded.misses[0]["url"] == "https://example.com/missing"
    drive_hit = loaded.find_drive_by_path(["folder", "file.txt"])
    assert drive_hit.body == b"hello drive"


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
