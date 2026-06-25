"""BrowseComp via PLAIN Claude Code — the A/B comparator that isolates the browser harness.

Same orchestrator + model as `agent_browser`, but NO agent-browser and NO Steel container: the
agent researches with Claude Code's OWN native tools (WebSearch / WebFetch). So agent_browser vs
claude_code answers "does driving a real browser beat plain Claude Code research, holding the
model fixed?" — the cleanest harness ablation on this benchmark.

Graded by the SAME answer-vs-reference text judge. Concurrency-safe (no shared browser daemon;
each task is an independent `claude -p`). `$0` extra — runs on your Claude subscription.
"""
import re

from benchmarks.browsecomp import config, tasks
from benchmarks.browsecomp.prompts import claude_code_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io

# Plain Claude Code research = its built-in web tools only (no agent-browser, no Bash/browser).
CLAUDE_CODE_TOOLS = ["WebSearch", "WebFetch"]
_CONF_RE = re.compile(r"CONFIDENCE:\s*(\d{1,3})", re.IGNORECASE)


def _extract_confidence(text):
    m = None
    for m in _CONF_RE.finditer(text or ""):
        pass  # take the LAST CONFIDENCE line
    return max(0, min(100, int(m.group(1)))) if m else None


def run(task, *, env, out, refs=None, dry=False):
    cmd = build_claude_cmd(
        claude_code_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        allowed_tools=CLAUDE_CODE_TOOLS,
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
        "answer": answer,
        "confidence": _extract_confidence(text),
    })
    return answer
