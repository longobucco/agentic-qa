"""BrowseComp runner: research ONE question with `claude -p` + agent-browser.

Uses the SAME containerized agent-browser harness as WebVoyager (the shared `core.steel`
Steel container -> concurrency-safe, headless), but the task is open-web RESEARCH and the
output is a short text answer + a stated confidence (captured for later calibration). No
screenshots are collected — BrowseComp is graded on the answer text, not visuals.
"""
import re

from benchmarks.browsecomp import config, tasks
from benchmarks.browsecomp.prompts import agent_docker_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io
from core.steel import steel_container, new_name

_CONF_RE = re.compile(r"CONFIDENCE:\s*(\d{1,3})", re.IGNORECASE)


def _extract_confidence(text):
    m = None
    for m in _CONF_RE.finditer(text or ""):
        pass  # take the LAST CONFIDENCE line
    if not m:
        return None
    return max(0, min(100, int(m.group(1))))


def run(task, *, env, out, refs=None, dry=False):
    """Run one research task; returns the parsed answer. Writes result.json (answer +
    confidence). The reference answer in `refs` is NEVER shown to the agent."""
    if dry:
        cmd = build_claude_cmd(agent_docker_prompt(task, new_name("bcab")),
                               model=config.MODEL or None, max_turns=config.MAX_TURNS)
        print("DRY-RUN command:\n ", preview(cmd))
        return None

    with steel_container("bcab") as cname:
        cmd = build_claude_cmd(
            agent_docker_prompt(task, cname),
            model=config.MODEL or None,
            max_turns=config.MAX_TURNS,
        )
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
