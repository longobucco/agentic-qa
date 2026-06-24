"""Shared config + Chrome-for-Testing discovery for the WebVoyager harness."""
import os
import glob
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
TASKS_FILE = DATA_DIR / "WebVoyager_data.jsonl"
REF_FILE = DATA_DIR / "reference_answer.json"

# Empty model => use the `claude` CLI default (your subscription's model).
MODEL = os.environ.get("WV_MODEL", "").strip()
PORT_BASE = int(os.environ.get("WV_PORT_BASE", "9222"))
MAX_TURNS = int(os.environ.get("WV_MAX_TURNS", "60"))      # claude -p turn cap (agent)
MAX_STEPS = int(os.environ.get("WV_MAX_STEPS", "25"))      # advisory browser-action cap
TASK_TIMEOUT = int(os.environ.get("WV_TASK_TIMEOUT", "900"))   # seconds per task
JUDGE_TIMEOUT = int(os.environ.get("WV_JUDGE_TIMEOUT", "180"))
HEADLESS = os.environ.get("WV_HEADED", "") == ""           # set WV_HEADED=1 to watch


def find_chrome() -> str:
    """Locate the Chrome for Testing binary installed by `agent-browser install`."""
    env = os.environ.get("WV_CHROME_BIN")
    if env and Path(env).exists():
        return env
    pattern = str(
        Path.home()
        / ".agent-browser/browsers/*/Google Chrome for Testing.app"
        "/Contents/MacOS/Google Chrome for Testing"
    )
    hits = sorted(glob.glob(pattern))
    if hits:
        return hits[-1]
    raise SystemExit(
        "Chrome for Testing not found. Run `agent-browser install`, "
        "or set WV_CHROME_BIN to a Chrome/Chromium binary."
    )
