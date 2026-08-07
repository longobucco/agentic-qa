"""OSWorld config from env. OSW_RELEASE picks the task track -- only "verified" (369 tasks,
xlang-ai/OSWorld) is wired up"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"

RELEASE = os.environ.get("OSW_RELEASE", "verified").strip().lower()
TASKS_FILE = DATA_DIR / f"osworld_{RELEASE}.jsonl"

MODEL = os.environ.get("WV_MODEL", "").strip()
MAX_TURNS = int(os.environ.get("OSW_MAX_TURNS", "150"))   # desktop GUI turns run >4x web-nav ones
MAX_STEPS = int(os.environ.get("OSW_MAX_STEPS", "30"))
TASK_TIMEOUT = int(os.environ.get("OSW_TASK_TIMEOUT", "3600"))

OBSERVATION = os.environ.get("OSW_OBSERVATION", "screenshot+a11y")
ACTION_SPACE = os.environ.get("OSW_ACTION_SPACE", "pyautogui")

IMAGE = os.environ.get(   # pinned by digest -- Daytona caches images by tag, not :latest
    "OSW_IMAGE",
    "ghcr.io/longobucco/osworld-ab@sha256:"
    "8917c3643b19f14d85aaa4f66ef87aa789854ae8529d6a6fe8151f0edc973f6b",
)
CONTROLLER_PORT = int(os.environ.get("OSW_CONTROLLER_PORT", "5000"))

CONTROLLER_URL = os.environ.get("OSW_CONTROLLER_URL", "").strip()   # skip provisioning
SANDBOX_ID = os.environ.get("OSW_SANDBOX_ID", "").strip()

# apps installed and validated in docker/Dockerfile.osworld; tasks.load_tasks() skips anything
# else by default (OSW_INCLUDE_ALL_APPS=1 overrides).
SUPPORTED_APPS = {
    "libreoffice_calc", "libreoffice_writer", "libreoffice_impress",
    "gimp", "thunderbird", "vlc", "chrome", "vscode",
}
ALWAYS_PRESENT_CAPABILITIES = {"terminal"}   # not an app -- always in the image
INCLUDE_ALL_APPS = bool(os.environ.get("OSW_INCLUDE_ALL_APPS", "").strip())
