import subprocess
from types import SimpleNamespace

from benchmarks.osworld.runners import common


def test_provenance_records_the_harness_commit():
    head = subprocess.run(["git", "-C", str(common.CHECKOUT_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    rec = common._provenance({"id": "t"}, SimpleNamespace(base_url=None), "2026-10-30T00:00:00")
    assert rec["harness_commit"] == head
    assert isinstance(rec["harness_dirty"], bool)


def test_outside_a_checkout_it_records_none(tmp_path):
    assert common._harness_revision(tmp_path) == {"harness_commit": None, "harness_dirty": None}
