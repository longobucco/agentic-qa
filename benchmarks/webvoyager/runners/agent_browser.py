"""WebVoyager runner: drive ONE task with `claude -p` + agent-browser over CDP.

The browser is provisioned by the environment (`core.environment.live_site_environment`),
which yields a CDP `port`; this runner only builds the prompt, invokes `claude -p`, parses
the answer, and writes `result.json`. Judging + `eval.json` are `core.run`'s job.
"""
from benchmarks.webvoyager import config, tasks
from benchmarks.webvoyager.prompts import agent_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io


def run(task, *, env, out, refs=None, dry=False):
    """Run one task; returns the parsed answer (None on --dry-run). Writes result.json."""
    # NOTE: agent-browser has ONE shared daemon and `connect` only binds the `default`
    # session, so this path is reliable only at concurrency=1. For parallelism, isolate the
    # daemon per worker (separate container / machine / remote provider), not a session name.
    cmd = build_claude_cmd(
        agent_prompt(task, env.port, out),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        add_dir=out,
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
        "web_name": task.get("web_name"),
        "ques": task["ques"],
        "web": task.get("web"),
        "answer": answer,
    })
    return answer
