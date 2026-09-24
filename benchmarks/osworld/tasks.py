"""Load OSWorld task specs; bucket by app under test."""
from benchmarks.osworld import config
from core.tasks import load_jsonl

_LOGIN_STEP_TYPES = {"login", "googledrive"}


def is_login_task(task):
    """Needs a real account login during setup -- excluded from the published 361-task
    OSWorld-Verified population (369 - 8 = 361)."""
    return any(s.get("type") in _LOGIN_STEP_TYPES for s in (task.get("config") or []))


def bucket_of(task):
    apps = task.get("related_apps") or []
    return apps[0] if apps else "misc"


def load_tasks():
    if not config.TASKS_FILE.exists():
        raise SystemExit(
            f"Missing {config.TASKS_FILE}. Run: "
            f"OSW_RELEASE={config.RELEASE} python -m benchmarks.osworld.data.download_data"
        )
    all_tasks = load_jsonl(config.TASKS_FILE)
    return [t for t in all_tasks if not is_login_task(t)]


def load_refs():
    return {}   # eval spec rides on the task
