"""Load OSWorld task specs; bucket by app under test."""
from benchmarks.osworld import config
from core.tasks import load_jsonl


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
    if config.INCLUDE_ALL_APPS:
        return all_tasks
    in_scope = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    out = [t for t in all_tasks if set(t.get("related_apps") or []) <= in_scope]
    skipped = len(all_tasks) - len(out)
    if skipped:
        print(f"[osworld] {len(out)} runnable tasks; skipped {skipped} outside the validated "
              f"app scope ({sorted(config.SUPPORTED_APPS)}) — set OSW_INCLUDE_ALL_APPS=1 to run "
              f"everything (expect ENVIRONMENT_ERROR, or a misdiagnosed FAILURE, outside this set)")
    return out


def load_refs():
    return {}   # eval spec rides on the task
