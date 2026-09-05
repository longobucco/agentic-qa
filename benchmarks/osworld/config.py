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
# G5 ARM_SELF_VERIFY (docs/g5-arm-self-verify-plan.md): appends a mandatory re-observe-and-check
# step to the prompt before the final ANSWER, targeting G8's false-completion and
# infeasibility-blindness findings. Off by default -- opt in per-run, not a permanent prompt change.
SELF_VERIFY = os.environ.get("OSW_SELF_VERIFY", "0") == "1"
# G5 idea #10 (docs/g5-arm-sandbox-enforcement-plan.md): the real fix for the artifact's
# sandbox-escape finding -- `--allowedTools` only suppresses the confirmation prompt, it doesn't
# restrict availability. `--disallowedTools Bash WebSearch WebFetch` + `--strict-mcp-config`
# (drops any MCP server not in --mcp-config, closing the "second, unrelated Playwright MCP
# server" half of the same finding) genuinely blocks the escape while leaving the OSWorld MCP
# tools intact -- verified live 2026-08-30 against a dummy MCP server: honest refusal, real
# ToolSearch "not found", no confabulation. `--tools ""` (tried first) was rejected: it also
# disables the legitimate MCP tools, not just Bash/Read/WebSearch, which starves the agent of
# any real capability and reliably produces confabulated fake tool calls instead -- see
# docs/finding-confabulation-under-tool-denial.md. Off by default -- opt in per-run.
ENFORCE_SANDBOX = os.environ.get("OSW_ENFORCE_SANDBOX", "0") == "1"
# G5 idea #11 (docs/g5-arm-restrict-run-python-plan.md): run_python (free-form pyautogui code) is
# called nearly as often as screenshot across real transcripts (2415 vs 3105 calls, 198
# transcripts, analysis/g5_run_python_usage.py) -- the agent defaults to scripting instead of the
# discrete click/type/key tools the harness is built to measure. `--disallowedTools
# mcp__osworld__run_python` removes just that one MCP tool from the toolset the model is even
# offered, leaving the other 10 OSWorld tools untouched -- verified live 2026-08-30 against a
# dummy MCP server: the model reports run_python as genuinely absent, no confabulated substitute,
# same clean-denial mechanism idea #10 already validated for Bash/WebSearch/WebFetch. Off by
# default -- opt in per-run.
RESTRICT_RUN_PYTHON = os.environ.get("OSW_RESTRICT_RUN_PYTHON", "0") == "1"

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
# "os": a generic desktop/OS-level capability tag (terminal use, file manager, ...) that shows
# up alongside a task's real app tag(s), e.g. ['vlc', 'os'] or ['vscode', 'os'] -- not an
# installable app, same category as "terminal". Found live 2026-08-19: treating it as an
# unsupported app was excluding 85 otherwise-runnable tasks from the population for no reason.
ALWAYS_PRESENT_CAPABILITIES = {"terminal", "os"}   # not apps -- always in the image
INCLUDE_ALL_APPS = bool(os.environ.get("OSW_INCLUDE_ALL_APPS", "").strip())
