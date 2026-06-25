"""WebArena config: the hosted site URLs + credentials, read from env so the SAME task
configs run against whatever stack you provisioned (local x86 box, or a Daytona sandbox).

WebArena task `start_url`s use placeholders (`__SHOPPING__`, `__REDDIT__`, ...). `tasks.py`
templates them to these URLs at load time. Set the ones for the sites you actually host;
unset sites simply can't run their tasks (`tasks.load_tasks()` skips them with a note).
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
TASKS_FILE = DATA_DIR / "webarena.jsonl"

# Placeholder -> hosted URL. Empty string = that site isn't provisioned.
SITE_URLS = {
    "__SHOPPING__": os.environ.get("WA_SHOPPING_URL", ""),
    "__SHOPPING_ADMIN__": os.environ.get("WA_SHOPPING_ADMIN_URL", ""),
    "__GITLAB__": os.environ.get("WA_GITLAB_URL", ""),
    "__REDDIT__": os.environ.get("WA_REDDIT_URL", ""),
    "__WIKIPEDIA__": os.environ.get("WA_WIKIPEDIA_URL", ""),
    "__MAP__": os.environ.get("WA_MAP_URL", ""),
    "__HOMEPAGE__": os.environ.get("WA_HOMEPAGE_URL", ""),
}
# placeholder -> site bucket name (for stratified reporting)
SITE_BUCKET = {
    "__SHOPPING__": "shopping", "__SHOPPING_ADMIN__": "shopping_admin",
    "__GITLAB__": "gitlab", "__REDDIT__": "reddit", "__WIKIPEDIA__": "wikipedia",
    "__MAP__": "map", "__HOMEPAGE__": "homepage",
}

MODEL = os.environ.get("WV_MODEL", "").strip()
MAX_TURNS = int(os.environ.get("WA_MAX_TURNS", os.environ.get("WV_MAX_TURNS", "60")))
TASK_TIMEOUT = int(os.environ.get("WA_TASK_TIMEOUT", "900"))


def provisioned_sites():
    """Buckets whose URL is set -> the sites whose tasks we can actually run."""
    return {SITE_BUCKET[ph] for ph, url in SITE_URLS.items() if url}
