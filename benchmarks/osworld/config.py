"""OSWorld config from env. OSW_RELEASE picks the task track -- only "verified" (369 tasks,
xlang-ai/OSWorld) is wired up"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"

RELEASE = os.environ.get("OSW_RELEASE", "verified").strip().lower()
TASKS_FILE = DATA_DIR / f"osworld_{RELEASE}.jsonl"

# The single most consequential knob in the harness, and until 2026-09-07 the ONLY one that was
# neither pinned nor recorded. With this unset, runners/agent_computer passes no --model, so every
# `claude -p` subprocess silently inherits whatever the CLI's current default happens to be -- and
# that default drifts (it also follows an interactive /model switch). Auditing `agent_model_usage`
# after the fact showed the G3 campaign had in fact run across THREE models: claude-sonnet-4-6
# (884 runs), claude-sonnet-5 (326) and claude-opus-4-8 (285). That silently breaks the
# "fixed model, variable harness" premise every comparison in this project rests on, so from here
# on the model is pinned explicitly, recorded in each run's provenance, and cross-checked against
# what the CLI reports it actually used (see agent_computer._provenance / _model_mismatch).
# OSW_MODEL is the name to use; WV_MODEL stays accepted for continuity with the other benchmarks.
MODEL = (os.environ.get("OSW_MODEL", "").strip()
         or os.environ.get("WV_MODEL", "").strip())

# Results live under <RESULTS_DIR>/<system>/, and `system` is the runner's name (see core.run).
# Deriving the runner name from the pinned model gives each model its own results tree for free,
# so a new pinned campaign can never overwrite the older mixed-model one -- which, having runs
# both with and without a saved transcript, has to be preserved exactly as it is.
_MODEL_SLUGS = {
    "claude-sonnet-5": "sonnet5",
    "claude-sonnet-4-6": "sonnet46",
    "claude-opus-4-8": "opus48",
}


def model_slug(model=None):
    model = MODEL if model is None else model
    if not model:
        return ""
    return _MODEL_SLUGS.get(model, model.replace("claude-", "").replace(".", "").replace("-", ""))


# "agent_computer" (unpinned, mixed-model, historical) vs "agent_computer_sonnet5" (pinned).
SYSTEM_NAME = f"agent_computer_{model_slug()}" if MODEL else "agent_computer"


def resolve_system(system=None):
    """Which results tree an offline analysis should read.

    Explicit argument wins; otherwise the pinned model decides, so
    `OSW_MODEL=claude-sonnet-5 python -m benchmarks.osworld.analysis.<x>` reads that model's
    tree instead of silently pooling it with the historical mixed-model one.
    """
    return system or SYSTEM_NAME


def system_from_argv(argv=None):
    """`--system <name>` for the analysis CLIs; None when absent (env/default then decides)."""
    import sys
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--system" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--system="):
            return a.split("=", 1)[1]
    return None

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

# Score with the evaluator tree fetched at data/download_data.py::UPSTREAM_COMMIT
# (data/download_evaluators.py) rather than whatever `desktop_env` release pip resolved. On by
# default once that tree is on disk; set OSW_PINNED_EVALUATORS=0 to keep a campaign scored by
# the installed release for the rest of its run, when mid-campaign homogeneity matters more
# than correctness on the six tasks the installed release can't score at all.
PINNED_EVALUATORS = os.environ.get("OSW_PINNED_EVALUATORS", "1") != "0"

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
