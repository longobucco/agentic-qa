"""WebVoyager task + reference-answer loading. Generic subsetting lives in `core.tasks`.

What's WebVoyager-specific and stays here: the bucket key (`web_name`) and the shape of
`reference_answer.json`. Everything else (filter / per-bucket / interleave) is `core.tasks`.
"""
from benchmarks.webvoyager.config import TASKS_FILE, REF_FILE
from core.tasks import load_jsonl


def bucket_of(task):
    """WebVoyager stratifies by site name."""
    return task.get("web_name") or task["id"].split("--")[0]


def load_tasks():
    if not TASKS_FILE.exists():
        raise SystemExit(
            f"Missing {TASKS_FILE}. Run: python -m benchmarks.webvoyager.download_data"
        )
    return load_jsonl(TASKS_FILE)


def get_task(task_id):
    for t in load_tasks():
        if t["id"] == task_id:
            return t
    raise SystemExit(f"Task id not found: {task_id}")


def load_refs():
    """Flatten reference_answer.json to {task_id: answer}.

    WebVoyager shape: {site: {"notice": ..., "answers": [{"id": N, "ans": ...}, ...]}}.
    Multiple 'possible' answers for one task are joined.
    """
    if not REF_FILE.exists():
        return {}
    import json
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
