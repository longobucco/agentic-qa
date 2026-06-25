"""Shared config for the BrowseComp harness. Reuses the WebVoyager env knobs where they
overlap so a head-to-head pins the same model/budget."""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
TASKS_FILE = DATA_DIR / "browsecomp.jsonl"

# Empty model => the `claude` CLI default. Shared with WebVoyager so an A/B pins one model.
MODEL = os.environ.get("WV_MODEL", "").strip()
MAX_TURNS = int(os.environ.get("BC_MAX_TURNS", os.environ.get("WV_MAX_TURNS", "60")))
TASK_TIMEOUT = int(os.environ.get("BC_TASK_TIMEOUT", "900"))   # seconds per research task
JUDGE_TIMEOUT = int(os.environ.get("BC_JUDGE_TIMEOUT", "120"))
