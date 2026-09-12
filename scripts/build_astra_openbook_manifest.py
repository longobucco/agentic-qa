"""Build the frozen open-book manifest: every task closed_book.classify() tags open-book, plus
which proxy site(s) it needs (docs/g_astra_open_book_runner_implementation.md's "Frozen
population"). Does NOT modify closed_book.py or its classification -- this only adds evidence on
top of the existing open-book/closed-book split (an explicit non-goal in the spec doc).

    python -m scripts.build_astra_openbook_manifest > benchmarks/osworld/astra_openbook_manifest.json

`external_live_getter` (get_cloud_file: a plain requests.get from the evaluator's own host
process) and `external_oauth_service` (get_googledrive_file: real pydrive2 OAuth2/Drive API
calls) mark tasks that need the HOST-side proxy (env/host_proxy.py) during scoring, on top of the
guest-side one every open-book task needs for the agent's own Chrome. The other three
EXTERNAL_LIVE_GETTERS entries (info_from_website/pdf_from_url/gotoRecreationPage_and_...) connect
to the SANDBOX's own Chrome via CDP (playwright.connect_over_cdp) -- already covered by the guest
proxy once Chrome is proxied, so they get an informational tag only, no separate handling.
"""
import hashlib
import json
import sys

from benchmarks.osworld import closed_book
from benchmarks.osworld.env.osworld_eval import EXTERNAL_LIVE_GETTERS
from benchmarks.osworld.tasks import load_tasks

_HOST_SIDE_GETTER_TAGS = {
    "cloud_file": "external_live_getter",
    "googledrive_file": "external_oauth_service",
}
_DEFAULT_SNAPSHOT_PROFILE = "web-v1"
SCHEMA_VERSION = 1


def _specs(ev, key):
    v = ev.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def getter_types(task):
    ev = task.get("evaluator") or {}
    types = set()
    for key in ("result", "expected"):
        for spec in _specs(ev, key):
            if spec and spec.get("type"):
                types.add(spec["type"])
    return sorted(types)


def proxy_tags(task):
    """Which proxy site(s) this task needs, beyond the guest-side Chrome proxy every open-book
    task gets. Returns a sorted list of tags -- empty for a task whose evaluator never leaves the
    sandbox at all."""
    tags = set()
    for getter in getter_types(task):
        if getter in _HOST_SIDE_GETTER_TAGS:
            tags.add(_HOST_SIDE_GETTER_TAGS[getter])
        elif getter in EXTERNAL_LIVE_GETTERS:
            tags.add("cdp_driven_live_getter")   # informational: guest proxy already covers it
    return sorted(tags)


def build_manifest_records(tasks):
    records = []
    for task in tasks:
        classification = closed_book.classify(task)
        if classification["book"] != "open-book":
            continue
        records.append({
            "task_id": classification["task_id"],
            "app": classification["app"],
            "reasons": classification["reasons"],
            "proxy_tags": proxy_tags(task),
            "snapshot_profile": _DEFAULT_SNAPSHOT_PROFILE,
        })
    return sorted(records, key=lambda r: r["task_id"])


def manifest_sha256(records):
    ids = "\n".join(r["task_id"] for r in records) + "\n"
    return hashlib.sha256(ids.encode()).hexdigest()


def build_manifest(source_release="osworld_verified"):
    records = build_manifest_records(load_tasks())
    return {
        "schema_version": SCHEMA_VERSION,
        "source_release": source_release,
        "classifier": "benchmarks.osworld.closed_book:classify",
        "count": len(records),
        "ids_sha256": manifest_sha256(records),
        "tasks": records,
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    manifest = build_manifest()
    if "--summary" in argv:
        by_tag = {}
        for record in manifest["tasks"]:
            for tag in (record["proxy_tags"] or ["none"]):
                by_tag[tag] = by_tag.get(tag, 0) + 1
        print(json.dumps({"count": manifest["count"], "by_proxy_tag": by_tag}, indent=2))
        return
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
