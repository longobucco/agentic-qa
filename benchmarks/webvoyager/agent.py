"""Run a single WebVoyager task: launch Chrome, drive it with `claude -p` + agent-browser."""
import json
import os
import re
import subprocess

from config import RESULTS_DIR, MODEL, MAX_TURNS, TASK_TIMEOUT, PORT_BASE
from chrome import Chrome
from prompts import agent_prompt

ANSWER_RE = re.compile(r"^ANSWER:\s*(.*)$", re.MULTILINE)


def _extract_answer(text: str) -> str:
    matches = ANSWER_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


def task_dir(task):
    return RESULTS_DIR / "agentbrowser" / task["id"]


def run_agent(task, port=PORT_BASE, headless=None):
    """Drive one task with Claude Code; returns (answer, output_dir). Writes result.json."""
    out = task_dir(task)
    out.mkdir(parents=True, exist_ok=True)

    kw = {} if headless is None else {"headless": headless}
    with Chrome(port, **kw):
        # NOTE: agent-browser has ONE shared daemon and `connect` only binds the
        # `default` session — named sessions can't attach. So this path is reliable
        # only at concurrency=1. For parallelism, isolate the daemon per worker
        # (separate container / machine / remote provider), not just a session name.
        cmd = [
            "claude", "-p", agent_prompt(task, port, out),
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--max-turns", str(MAX_TURNS),
            "--add-dir", str(out),
        ]
        if MODEL:
            cmd += ["--model", MODEL]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TASK_TIMEOUT
            )
            raw = proc.stdout
        except subprocess.TimeoutExpired as e:
            raw = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")

    text = raw
    try:
        text = json.loads(raw).get("result", raw)
    except Exception:
        pass

    answer = _extract_answer(text)
    (out / "agent_output.txt").write_text(text or "")
    (out / "result.json").write_text(json.dumps({
        "id": task["id"],
        "web_name": task.get("web_name"),
        "ques": task["ques"],
        "web": task.get("web"),
        "answer": answer,
    }, indent=2))
    return answer, out


if __name__ == "__main__":
    import sys
    from tasks import load_tasks, get_task
    tid = sys.argv[1] if len(sys.argv) > 1 else None
    t = get_task(tid) if tid else load_tasks()[0]
    ans, d = run_agent(t)
    print(f"[{t['id']}] ANSWER: {ans!r}\n -> {d}")
