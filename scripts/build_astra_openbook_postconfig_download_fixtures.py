"""Author real fixture content for a THIRD, previously-undiscovered download location: a
'download' config step nested inside the evaluator's OWN postconfig
(task['evaluator']['postconfig']), run by env.osworld_eval._score right before scoring -- e.g.
to fetch a fresh reference copy of a file the agent was supposed to have downloaded, then diff
it against what's on disk. Distinct from both:
  - config-level download steps (build_astra_openbook_download_fixtures.py)
  - evaluator result/expected cloud_file specs (build_astra_openbook_goldref_fixtures.py)

Confirmed live 2026-09-14: task 415ef462 scored EVAL_ERROR with "Setup step N failed:
_download_setup - Failed to download ... invoice0123456789-2312.pdf. No retries left." -- a URL
that appears NOWHERE in the task's own top-level config, only inside evaluator.postconfig's own
download step (top-level task['postconfig'] is null for this task; easy to miss by checking only
that field). 10 of the 258 curated population tasks have this gap.

    python -m scripts.build_astra_openbook_postconfig_download_fixtures

Same trust basis as the other two curation passes: every URL found this way also resolves to
huggingface.co/datasets/xlangai/ubuntu_osworld_file_cache. Idempotent and additive: merges into
whatever bundle a task already has, touching only these postconfig-download URLs.
"""
import json
import sys

from benchmarks.osworld import closed_book
from benchmarks.osworld.openbook_proxy import bundle as bundle_mod
from benchmarks.osworld.tasks import load_tasks
from scripts.build_astra_openbook_goldref_fixtures import _fetch, _content_type_for

_FIXTURES_ROOT = "benchmarks/osworld/openbook_fixtures"


def _postconfig_download_urls(task):
    ev = task.get("evaluator") or {}
    urls = []
    for step in ev.get("postconfig") or []:
        if str(step.get("type", "")).lower() != "download":
            continue
        for f in step.get("parameters", {}).get("files", []):
            u = f.get("url")
            if u:
                urls.append(u)
    return urls


def _urls_by_task():
    by_task = {}
    for task in load_tasks():
        classification = closed_book.classify(task)
        if classification["book"] != "open-book":
            continue
        urls = _postconfig_download_urls(task)
        if urls:
            by_task[classification["task_id"]] = urls
    return by_task


def _write_task_bundle(task_id, urls, url_cache):
    bundle_dir = f"{_FIXTURES_ROOT}/{task_id}"
    try:
        existing = bundle_mod.FixtureBundle.load(bundle_dir)
        manifest = dict(existing.manifest)
    except (FileNotFoundError, ValueError):
        manifest = {"schema_version": bundle_mod.SCHEMA_VERSION, "task_id": task_id,
                   "http": [], "drive": []}

    by_key = {(e["method"], e["url"]): e for e in manifest.get("http", [])}
    bodies = {}
    new_bytes = 0
    for url in urls:
        already = by_key.get(("GET", url))
        body = _fetch(url, url_cache)
        entry = {
            "method": "GET", "url": url, "status": 200,
            "headers": {"content-type": _content_type_for(url)},
            "body_sha256": __import__("hashlib").sha256(body).hexdigest(),
            "body_file": f"bodies/{__import__('hashlib').sha256(body).hexdigest()}",
        }
        by_key[("GET", url)] = entry
        bodies[entry["body_file"]] = body
        if already is None:
            new_bytes += len(body)

    manifest["http"] = list(by_key.values())
    bundle_mod.write_bundle(bundle_dir, manifest, bodies)
    return new_bytes


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    by_task = _urls_by_task()
    url_cache = {}
    total_new_bytes = 0
    tasks_touched = 0
    for task_id in sorted(by_task):
        new_bytes = _write_task_bundle(task_id, by_task[task_id], url_cache)
        total_new_bytes += new_bytes
        tasks_touched += 1
    summary = {"tasks_touched": tasks_touched, "unique_urls_fetched": len(url_cache),
              "new_body_bytes": total_new_bytes}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
