"""Generic task loading + subsetting, keyed by an arbitrary `bucket`.

A *bucket* is whatever a benchmark stratifies and reports by: WebVoyager's `web_name`,
BrowseComp's topic, WebArena's site. Every helper here takes a `bucket_of(task) -> str`, so
nothing is hardcoded to `site` — that generalization is what lets one set of subset/
interleave/per-bucket logic serve all three benchmarks.
"""
import json


def load_jsonl(path):
    """Load a `.jsonl` task file into a list of dicts (blank lines skipped)."""
    tasks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def default_bucket(task):
    """Fallback bucket: `web_name`, else the prefix of an `id` like `Amazon--3`."""
    return task.get("web_name") or task["id"].split("--")[0]


def filter_tasks(tasks, *, bucket_of=default_bucket, buckets=None, ids=None,
                 limit=None, per_bucket=None, interleave=False):
    """Subset a task list: by bucket, by id, capped per bucket, interleaved, and/or limited.

    Order of operations matters: bucket/id filters first, then the per-bucket stratified
    cap, then interleave (round-robin across buckets), then a final hard limit.
    """
    out = tasks
    if buckets:
        want = {b.lower() for b in buckets}
        out = [t for t in out if bucket_of(t).lower() in want]
    if ids:
        idset = set(ids)
        out = [t for t in out if t["id"] in idset]
    if per_bucket:
        seen = {}
        keep = []
        for t in out:
            b = bucket_of(t)
            if seen.get(b, 0) < per_bucket:
                seen[b] = seen.get(b, 0) + 1
                keep.append(t)
        out = keep
    if interleave:
        out = _interleave(out, bucket_of)
    if limit:
        out = out[:limit]
    return out


def _interleave(tasks, bucket_of):
    """Round-robin across buckets so concurrent workers don't hammer one domain."""
    buckets = {}
    for t in tasks:
        buckets.setdefault(bucket_of(t), []).append(t)
    order = list(buckets.values())
    out = []
    while any(order):
        for b in order:
            if b:
                out.append(b.pop(0))
    return out
