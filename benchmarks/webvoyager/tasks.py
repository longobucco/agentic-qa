"""Load WebVoyager tasks + reference answers, with subset/interleave helpers."""
import json

from config import TASKS_FILE, REF_FILE


def load_tasks():
    if not TASKS_FILE.exists():
        raise SystemExit(f"Missing {TASKS_FILE}. Run: python download_data.py")
    tasks = []
    with open(TASKS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def load_refs():
    """Flatten reference_answer.json to {task_id: answer}.

    WebVoyager shape: {site: {"notice": ..., "answers": [{"id": N, "ans": ...}, ...]}}.
    Multiple 'possible' answers for one task are joined.
    """
    if not REF_FILE.exists():
        return {}
    raw = json.loads(REF_FILE.read_text())
    flat = {}
    if isinstance(raw, dict):
        for site, block in raw.items():
            answers = block.get("answers", []) if isinstance(block, dict) else block
            if not isinstance(answers, list):
                continue
            by_id = {}
            for entry in answers:
                if isinstance(entry, dict) and "id" in entry:
                    by_id.setdefault(entry["id"], []).append(_ans_str(entry))
            for idx, anss in by_id.items():
                flat[f"{site}--{idx}"] = " | ".join(a for a in anss if a)
    return flat


def _ans_str(val):
    if isinstance(val, dict):
        for k in ("ans", "answer", "reference_answer", "gpt_answer"):
            if k in val:
                return str(val[k])
        return ""
    return str(val)


def get_task(task_id):
    for t in load_tasks():
        if t["id"] == task_id:
            return t
    raise SystemExit(f"Task id not found: {task_id}")


def filter_tasks(tasks, site=None, ids=None, limit=None, per_site=None, interleave=False):
    out = tasks
    if site:
        sset = {s.lower() for s in site}
        out = [t for t in out if (t.get("web_name") or t["id"].split("--")[0]).lower() in sset]
    if ids:
        idset = set(ids)
        out = [t for t in out if t["id"] in idset]
    if per_site:
        seen = {}
        keep = []
        for t in out:
            s = t.get("web_name") or t["id"].split("--")[0]
            if seen.get(s, 0) < per_site:
                seen[s] = seen.get(s, 0) + 1
                keep.append(t)
        out = keep
    if interleave:
        out = _interleave_by_site(out)
    if limit:
        out = out[:limit]
    return out


def _interleave_by_site(tasks):
    """Round-robin across sites so concurrent workers don't hammer one domain."""
    buckets = {}
    for t in tasks:
        buckets.setdefault(t.get("web_name") or t["id"].split("--")[0], []).append(t)
    order = list(buckets.values())
    out = []
    while any(order):
        for b in order:
            if b:
                out.append(b.pop(0))
    return out
