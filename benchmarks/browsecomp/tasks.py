"""BrowseComp task + reference loading. Generic subsetting lives in `core.tasks`.

BrowseComp-specific: the bucket key is the question's `problem_topic`, and the reference
answer rides INSIDE the task jsonl (the agent never sees it; only the grader does).
"""
from benchmarks.browsecomp.config import TASKS_FILE
from core.tasks import load_jsonl


def bucket_of(task):
    """BrowseComp stratifies by topic."""
    return task.get("bucket") or "all"


def load_tasks():
    if not TASKS_FILE.exists():
        raise SystemExit(
            f"Missing {TASKS_FILE}. Run: python -m benchmarks.browsecomp.data.download_data"
        )
    return load_jsonl(TASKS_FILE)


def load_refs():
    """{task_id: reference_answer} — the grader's gold string, withheld from the agent."""
    return {t["id"]: t.get("answer", "") for t in load_tasks()}
