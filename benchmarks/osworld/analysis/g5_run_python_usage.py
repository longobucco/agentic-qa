"""G5 idea #11 task selection -- per-task run_python call counts, re-derived from real
conversation.jsonl transcripts already on disk (analysis/conversation_coverage.py's "real" set,
agent_api_error_status unset -- excludes rate-limit stubs).

The idea's own finding ("run_python called 2007 times across 179 real transcripts, nearly as
often as screenshot's 2406") was a one-off count from an earlier session pass, not a saved
script -- this re-derives it properly so idea #11's task selection ("tasks with heavy run_python
use in their existing transcripts, so a shift is visible") is reproducible rather than
re-eyeballed.

Counts tool_use blocks by MCP tool name (the `name` field on an assistant message's tool_use
content block is "mcp__osworld__<tool>", matching runners/agent_computer.OSWORLD_TOOLS).

Run: python -m benchmarks.osworld.analysis.g5_run_python_usage [--json]
"""
import collections
import json
import sys

from benchmarks.osworld import config
from benchmarks.osworld.analysis.conversation_coverage import coverage
from benchmarks.osworld.tasks import load_tasks

TOOL_PREFIX = "mcp__osworld__"


def _tool_counts(conversation_path):
    """Counter of tool name -> call count for one conversation.jsonl."""
    counts = collections.Counter()
    try:
        lines = conversation_path.read_text().splitlines()
    except Exception:
        return counts
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = (block.get("name") or "")
                if name.startswith(TOOL_PREFIX):
                    counts[name[len(TOOL_PREFIX):]] += 1
    return counts


def scan(results_dir=None, system=None):
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    cov = coverage(results_dir, system)
    tasks = {t["id"]: t for t in load_tasks()}

    per_task = {}
    total = collections.Counter()
    n_transcripts = 0
    for tid, info in cov.items():
        task_total = collections.Counter()
        for n in info["real"]:
            path = base / tid / f"run_{n}" / "conversation.jsonl"
            c = _tool_counts(path)
            task_total += c
            total += c
            n_transcripts += 1
        if task_total:
            per_task[tid] = {
                "app": info.get("app", "?"),
                "n_real_transcripts": len(info["real"]),
                "counts": dict(task_total),
                "run_python": task_total.get("run_python", 0),
                "screenshot": task_total.get("screenshot", 0),
            }
    return {"n_transcripts": n_transcripts, "totals": dict(total), "per_task": per_task}


def _main():
    result = scan(system=config.system_from_argv())
    if "--json" in sys.argv:
        print(json.dumps(result, indent=2))
        return
    print(f"{result['n_transcripts']} real transcripts, {len(result['per_task'])} tasks with "
          f"at least one OSWorld tool call")
    print("totals:", dict(sorted(result["totals"].items(), key=lambda kv: -kv[1])))
    ranked = sorted(result["per_task"].items(), key=lambda kv: -kv[1]["run_python"])
    print("\ntop 15 tasks by run_python calls:")
    for tid, info in ranked[:15]:
        print(f"  {tid[:8]} {info['app']:20s} run_python={info['run_python']:4d} "
              f"screenshot={info['screenshot']:4d} n_transcripts={info['n_real_transcripts']}")


if __name__ == "__main__":
    _main()
