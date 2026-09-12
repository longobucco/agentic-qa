"""Classify OSWorld tasks for the closed-book Astra campaign.

Closed-book means the agent can solve the instruction entirely from the freshly provisioned
desktop and its visible applications.  Browser/network tasks, explicit URLs, and setup that
depends on external services are deferred to a separately labelled open-book campaign.
"""
import json
import re
import sys

from benchmarks.osworld.tasks import app_of, load_tasks

_URL = re.compile(r"https?://|www\\.", re.I)
_EXTERNAL_SETUP = ("download", "googledrive", "login", "drive")


def reasons(task):
    out = []
    instruction = task.get("instruction") or ""
    if _URL.search(instruction):
        out.append("instruction_contains_url")
    if app_of(task) == "chrome":
        out.append("browser_task")
    for step in task.get("config") or []:
        kind = str(step.get("type", "")).lower()
        if any(token in kind for token in _EXTERNAL_SETUP):
            out.append(f"external_setup:{kind}")
    return sorted(set(out))


def classify(task):
    why = reasons(task)
    return {"task_id": task["id"], "app": app_of(task),
            "book": "open-book" if why else "closed-book", "reasons": why}


def main(argv=None):
    argv = argv or sys.argv[1:]
    tasks = load_tasks()
    by_id = {task["id"]: task for task in tasks}
    if len(argv) == 2 and argv[0] == "--id":
        print(classify(by_id[argv[1]])["book"])
        return
    rows = [classify(task) for task in tasks]
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
