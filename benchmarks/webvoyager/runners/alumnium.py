"""WebVoyager runner: re-run a task through the ALUMNIUM pipeline (single OpenAI key).

Architecture (mirrors Alumnium's published WebVoyager run, adapted to one key):
  - Orchestrator : Claude Code (`claude -p`) on your subscription           (no key)
  - Browser hands: Alumnium MCP (`uvx alumnium mcp`) -> Selenium Chrome,
                   internal do/check model = OpenAI                          (OPENAI_API_KEY)
  - Judge        : our canonical WebVoyager screenshot judge (evaluate.py)   (no key)

Alumnium gives each `claude -p` its OWN MCP + Selenium Chrome (no shared daemon), so this
runner is concurrency-safe and needs no browser from the environment. The OpenAI key is read
from the environment and inherited by the MCP subprocess (never written to disk).

Screenshots (so the canonical judge scores this runner like every other): exactly as
Alumnium's own WebVoyager harness does it, we set `ALUMNIUM_FULL_PAGE_SCREENSHOT=true` so the
MCP auto-saves a full-page screenshot on every do/get/check, and `ALUMNIUM_PLANNER=false` so
Claude plans while Alumnium executes single actions. The MCP writes those frames to its
container-local store (`/root/.alumnium/<driver>/screenshots/NN-*.png`) — kept container-
local on purpose, because Chrome fails to start when its driver writes under a Docker bind
mount. After the task we `docker cp` the screenshots out of the (named, non-`--rm`) container
into `out` as `step_N.png`, where evaluate.py's screenshot judge reads them.

Deviations from their exact run (document in the paper): OpenAI (not Azure) for the do/check
model; Claude (not GPT-5) as the judge.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from benchmarks.webvoyager import config, tasks
from benchmarks.webvoyager.prompts import alumnium_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io

ALUMNIUM_TOOLS = [
    "mcp__alumnium__start_driver", "mcp__alumnium__do", "mcp__alumnium__check",
    "mcp__alumnium__get", "mcp__alumnium__wait", "mcp__alumnium__stop_driver",
]

# Env passed into the MCP so Alumnium captures screenshots the way their published run did.
_ALUMNIUM_ENV = {
    "ALUMNIUM_FULL_PAGE_SCREENSHOT": "true",   # full-page screenshot auto-saved per action
    "ALUMNIUM_PLANNER": "false",               # Claude plans; Alumnium executes ONE action
    "ALUMNIUM_DRIVER": "selenium",
}
# Alumnium's default store is `.alumnium` RELATIVE to the process CWD, and the image's
# WORKDIR is /work -> screenshots land at /work/.alumnium/artifacts/<id>/screenshots/. Kept
# off any bind mount (Chrome won't start with its driver writing under a Docker virtiofs
# mount); we docker-cp it out of the named container after the task.
_CONTAINER_STORE = "/work/.alumnium"
_IMG_EXTS = (".png", ".jpg", ".jpeg")
_MAX_SHOTS = 8           # keep only the last K frames (the judge scores the end state)

_cfg_lock = threading.Lock()
_native_cfg = None


def preflight():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY not set. `export OPENAI_API_KEY=sk-...` first "
            "(needed by the Alumnium MCP's do/check model)."
        )


def _image():
    return os.environ.get("WV_ALUMNIUM_IMAGE", "alumnium-mcp:latest")


def _write_cfg(spec):
    """Write a one-server MCP config to a temp file; return its path (no secret on disk)."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mcpServers": {"alumnium": spec}}, f)
    f.close()
    return f.name


def _native_mcp_config():
    """Local `uvx alumnium mcp` (its Chrome opens a VISIBLE window). Cached per process."""
    global _native_cfg
    with _cfg_lock:
        if _native_cfg is None:
            _native_cfg = _write_cfg({
                "type": "stdio", "command": "uvx", "args": ["alumnium", "mcp"],
            })
    return _native_cfg


def _docker_spec(cname):
    """Per-task containerized MCP. Alumnium's headed Chromium renders to the image's virtual
    Xvfb display (invisible on the host). The container is NAMED and NOT `--rm`, so after the
    task we can `docker cp` its screenshots out before removing it. OPENAI_API_KEY is
    forwarded by name; the screenshot/planner env is set explicitly. One container per task
    keeps the no-shared-daemon, concurrency-safe property."""
    args = ["run", "-i", "--name", cname, "-e", "OPENAI_API_KEY"]
    for k, v in _ALUMNIUM_ENV.items():
        args += ["-e", f"{k}={v}"]
    args.append(_image())
    return {"type": "stdio", "command": "docker", "args": args}


def _collect_screenshots(cname, out):
    """Copy Alumnium's per-step screenshots out of the (stopped) container into `out` as
    step_N.png (last `_MAX_SHOTS`) so the canonical screenshot judge can see them."""
    tmp = tempfile.mkdtemp(prefix="alum_cp_", dir="/tmp")
    try:
        subprocess.run(["docker", "cp", f"{cname}:{_CONTAINER_STORE}/.", tmp],
                       timeout=60, capture_output=True)
        src = sorted(p for p in Path(tmp).rglob("*")
                     if p.is_file() and p.parent.name == "screenshots"
                     and p.suffix.lower() in _IMG_EXTS)[-_MAX_SHOTS:]
        Path(out).mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(src, 1):
            shutil.copy(p, Path(out) / f"step_{i}{p.suffix.lower()}")
        return len(src)
    except Exception:
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rm_container(cname):
    try:
        subprocess.run(["docker", "rm", "-f", cname], timeout=30, capture_output=True)
    except Exception:
        pass


def run(task, *, env, out, refs=None, dry=False):
    """Run one task via the Alumnium MCP; returns the answer. Writes result.json (+ in Docker
    mode, collects Alumnium's screenshots into `out` for the judge)."""
    docker = bool(os.environ.get("WV_ALUMNIUM_DOCKER"))
    cname = "alum_" + uuid.uuid4().hex[:12] if docker else None
    mcp_config = _write_cfg(_docker_spec(cname)) if docker else _native_mcp_config()

    cmd = build_claude_cmd(
        alumnium_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        mcp_config=mcp_config,
        allowed_tools=ALUMNIUM_TOOLS,
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        return None

    try:
        text = run_claude(cmd, timeout=config.TASK_TIMEOUT)
        answer = extract_answer(text)
        results_io.write_output(out, text)
        if docker:
            _collect_screenshots(cname, out)
        results_io.write_result(out, {
            "id": task["id"],
            "bucket": tasks.bucket_of(task),
            "ques": task["ques"],
            "web": task.get("web"),
            "answer": answer,
        })
        return answer
    finally:
        if docker:
            _rm_container(cname)
