"""WebVoyager runner: re-run a task through the ALUMNIUM pipeline (single OpenAI key).

Architecture (mirrors Alumnium's published 98.5% run, adapted to one key):
  - Orchestrator : Claude Code (`claude -p`) on your subscription           (no key)
  - Browser hands: Alumnium MCP (`uvx alumnium mcp`) -> Selenium Chrome,
                   internal do/check model = OpenAI                          (OPENAI_API_KEY)
  - Judge        : our subscription Claude judge (evaluate.py)               (no key)

Alumnium gives each `claude -p` its OWN MCP + Selenium Chrome (no shared daemon), so this
runner is concurrency-safe and needs no browser from the environment. The OpenAI key is read
from the environment and inherited by the MCP subprocess (never written to disk).

Deviations from their exact run (document in the paper): OpenAI (not Azure) for the do/check
model; Claude (not GPT-5) as judge; no per-step screenshots (Alumnium MCP exposes no
screenshot tool), so the judge scores the answer vs the reference.
"""
import json
import os
import tempfile
import threading

from benchmarks.webvoyager import config, tasks
from benchmarks.webvoyager.prompts import alumnium_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io

ALUMNIUM_TOOLS = [
    "mcp__alumnium__start_driver", "mcp__alumnium__do", "mcp__alumnium__check",
    "mcp__alumnium__get", "mcp__alumnium__wait", "mcp__alumnium__stop_driver",
]

_cfg_lock = threading.Lock()
_mcp_cfg = None


def preflight():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY not set. `export OPENAI_API_KEY=sk-...` first "
            "(needed by the Alumnium MCP's do/check model)."
        )


def _mcp_config():
    """Write a temp MCP config once per process (no secret; key inherited from env)."""
    global _mcp_cfg
    with _cfg_lock:
        if _mcp_cfg is None:
            cfg = {"mcpServers": {"alumnium": {
                "type": "stdio", "command": "uvx", "args": ["alumnium", "mcp"],
            }}}
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(cfg, f)
            f.close()
            _mcp_cfg = f.name
    return _mcp_cfg


def run(task, *, env, out, refs=None, dry=False):
    """Run one task via the Alumnium MCP; returns the answer. Writes result.json."""
    cmd = build_claude_cmd(
        alumnium_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        mcp_config=_mcp_config(),
        allowed_tools=ALUMNIUM_TOOLS,
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        return None

    text = run_claude(cmd, timeout=config.TASK_TIMEOUT)
    answer = extract_answer(text)
    results_io.write_output(out, text)
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "ques": task["ques"],
        "web": task.get("web"),
        "answer": answer,
    })
    return answer
