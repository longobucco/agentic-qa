"""Author real fixture content for every open-book task's "download" config step
(SetupController._download_setup, host-side -- see host_proxy.py/build_astra_openbook_manifest.py's
`host_side_config_download` tag). Before this script ran, only 2 of the 251 download-tagged tasks
(closed_book.py's 'external_setup:download' reason) had any fixture authored at all -- the
mechanism (host_proxy wrapping + non-Chrome egress) was proven generically, but running any of the
other ~249 for real would hit a `fixture_miss` 502 on the task's own declared download URL.

Confirmed live 2026-09-13: all 419 download-step file references across the 251 tasks resolve to
exactly one source -- huggingface.co/datasets/xlangai/ubuntu_osworld_file_cache (386 unique URLs,
all HTTP 200, none broken; ~4.9MB total, largest single file ~128KB). This is OSWorld's own
upstream file-cache dataset (the same one real, un-proxied OSWorld runs already depend on), not
arbitrary live internet -- downloading it once and freezing the exact bytes as fixture content is
strictly higher-fidelity than a synthetic placeholder, and (being <5MB total) cheap enough to do
for the whole population rather than a curated subset.

    python -m scripts.build_astra_openbook_download_fixtures

Idempotent and additive: re-running only touches the `http` entries for a task's own download
URLs, preserving any other fixture entries (drive, chrome-getter http) already authored in that
task's bundle directory (see the merge in `_write_task_bundle`).
"""
import json
import mimetypes
import sys
import urllib.request

from benchmarks.osworld import closed_book
from benchmarks.osworld.openbook_proxy import bundle as bundle_mod
from benchmarks.osworld.tasks import load_tasks
from scripts.build_astra_openbook_manifest import config_step_types

_FIXTURES_ROOT = "benchmarks/osworld/openbook_fixtures"


def _download_files_by_task():
    by_task = {}
    for task in load_tasks():
        classification = closed_book.classify(task)
        if classification["book"] != "open-book":
            continue
        if "download" not in config_step_types(task):
            continue
        task_id = classification["task_id"]
        for step in task.get("config") or []:
            if str(step.get("type", "")).lower() != "download":
                continue
            for f in step.get("parameters", {}).get("files", []):
                url, path = f.get("url", ""), f.get("path", "")
                if url and path:
                    by_task.setdefault(task_id, []).append((url, path))
    return by_task


def _fetch(url, cache):
    if url in cache:
        return cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    cache[url] = body
    return body


def _content_type_for(path):
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _write_task_bundle(task_id, files, url_cache):
    bundle_dir = f"{_FIXTURES_ROOT}/{task_id}"
    try:
        existing = bundle_mod.FixtureBundle.load(bundle_dir)
        manifest = dict(existing.manifest)
    except (FileNotFoundError, ValueError):
        manifest = {"schema_version": bundle_mod.SCHEMA_VERSION, "task_id": task_id,
                   "http": [], "drive": []}

    by_key = {(e["method"], e["url"]): e for e in manifest.get("http", [])}
    bodies = {}
    for url, path in files:
        body = _fetch(url, url_cache)
        entry = {
            "method": "GET", "url": url, "status": 200,
            "headers": {"content-type": _content_type_for(path)},
            "body_sha256": __import__("hashlib").sha256(body).hexdigest(),
            "body_file": f"bodies/{__import__('hashlib').sha256(body).hexdigest()}",
        }
        by_key[("GET", url)] = entry
        bodies[entry["body_file"]] = body

    manifest["http"] = list(by_key.values())
    bundle_mod.write_bundle(bundle_dir, manifest, bodies)
    return len(files), sum(len(b) for b in bodies.values())


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    by_task = _download_files_by_task()
    url_cache = {}
    total_bytes = 0
    for task_id in sorted(by_task):
        n_files, n_bytes = _write_task_bundle(task_id, by_task[task_id], url_cache)
        total_bytes += n_bytes
    summary = {"tasks_authored": len(by_task), "unique_urls_fetched": len(url_cache),
              "total_body_bytes": total_bytes}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
