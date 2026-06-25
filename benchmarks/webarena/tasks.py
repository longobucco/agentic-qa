"""WebArena task loading: read vendored configs, TEMPLATE site placeholders to the hosted
URLs, and keep only tasks whose sites are provisioned. Generic subsetting is `core.tasks`.

Each task carries its own deterministic `eval` spec (string/url/program_html), so `load_refs`
is trivial — the "reference" lives on the task and `evaluate.py` reads it directly.
"""
from benchmarks.webarena import config
from core.tasks import load_jsonl


def _template(s):
    for ph, url in config.SITE_URLS.items():
        if url:
            s = s.replace(ph, url)
    return s


def bucket_of(task):
    """WebArena stratifies by site."""
    sites = task.get("sites") or []
    return sites[0] if sites else "unknown"


def load_tasks():
    if not config.TASKS_FILE.exists():
        raise SystemExit(
            f"Missing {config.TASKS_FILE}. Run: python -m benchmarks.webarena.data.download_data"
        )
    have = config.provisioned_sites()
    out = []
    skipped = 0
    for t in load_jsonl(config.TASKS_FILE):
        sites = set(t.get("sites") or [])
        if not sites or not sites.issubset(have):
            skipped += 1
            continue                       # can't run tasks for un-provisioned sites
        t["start_url"] = _template(t.get("start_url", ""))
        out.append(t)
    if skipped:
        print(f"[webarena] {len(out)} runnable tasks; skipped {skipped} (sites not provisioned: "
              f"set WA_*_URL for {sorted({s for t in load_jsonl(config.TASKS_FILE) for s in (t.get('sites') or []) if s not in have})})")
    return out


def load_refs():
    """Eval spec rides on the task; no separate reference map needed."""
    return {}
