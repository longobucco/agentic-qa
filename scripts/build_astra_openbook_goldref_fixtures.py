"""Author real fixture content for the evaluator's own "gold reference" cloud_file URLs on
every 'external_live_getter'-tagged open-book task -- a SEPARATE set of URLs from the
config-level 'download' step (scripts/build_astra_openbook_download_fixtures.py), confirmed
live 2026-09-13 to be a real, previously-undiscovered gap: a task tagged BOTH
external_live_getter and host_side_config_download had its config-download fixture authored,
but not its evaluator's own comparison file, so scoring hit an honest 502 fixture_miss from our
own proxy (correctly refusing to leak to the real huggingface.co, but blocking every such task
from ever scoring for real).

    python -m scripts.build_astra_openbook_goldref_fixtures

Same trust basis as the config-download curation: every one of these 225 URLs (176 tasks)
resolves to huggingface.co/datasets/xlangai/ubuntu_osworld_file_cache -- OSWorld's own upstream
file-cache dataset, not arbitrary live internet. Idempotent and additive: merges into whatever
bundle a task already has (from download curation or otherwise), touching only its own
gold-reference URLs.
"""
import json
import sys
import urllib.parse
import urllib.request

from benchmarks.osworld import closed_book
from benchmarks.osworld.openbook_proxy import bundle as bundle_mod
from benchmarks.osworld.tasks import load_tasks

_FIXTURES_ROOT = "benchmarks/osworld/openbook_fixtures"


def _specs(ev, key):
    v = ev.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _cloud_file_urls(task):
    ev = task.get("evaluator") or {}
    urls = []
    for key in ("result", "expected"):
        for spec in _specs(ev, key):
            if not spec or spec.get("type") != "cloud_file":
                continue
            path = spec.get("path")
            urls.extend(u for u in (path if isinstance(path, list) else [path]) if u)
    return urls


def _goldref_urls_by_task():
    by_task = {}
    for task in load_tasks():
        classification = closed_book.classify(task)
        if classification["book"] != "open-book":
            continue
        urls = _cloud_file_urls(task)
        if urls:
            by_task[classification["task_id"]] = urls
    return by_task


def _safe_url(url):
    """Some task-authored URLs carry a raw (non-percent-encoded) unicode filename -- http.client
    can only send ASCII request lines, so encode the path/query, leaving already-percent-encoded
    sequences (the common case, e.g. '%20') alone via safe='/%'."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%")
    query = urllib.parse.quote(parts.query, safe="=&%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _fetch(url, cache):
    if url in cache:
        return cache[url]
    req = urllib.request.Request(_safe_url(url), headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    cache[url] = body
    return body


def _content_type_for(url):
    lower = url.lower()
    for ext, ctype in ((".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                      (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                      (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                      (".csv", "text/csv"), (".txt", "text/plain"), (".json", "application/json"),
                      (".pdf", "application/pdf"), (".png", "image/png"),
                      (".jpg", "image/jpeg"), (".jpeg", "image/jpeg")):
        if lower.endswith(ext):
            return ctype
    return "application/octet-stream"


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
    by_task = _goldref_urls_by_task()
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
