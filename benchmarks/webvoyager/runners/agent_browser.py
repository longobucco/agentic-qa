"""WebVoyager runner: drive ONE task with `claude -p` + agent-browser.

Two modes:
  - **native** (default): the environment (`core.environment.live_site_environment`) launches
    a throwaway Chrome and yields its CDP `port`; the agent runs `agent-browser connect
    <port>` on the host. agent-browser's daemon is a singleton bound to the `default` session,
    so this path is reliable only at **concurrency 1**.
  - **`WV_AGENT_DOCKER=1`**: each task gets its OWN Steel container (own agent-browser daemon
    + own Chromium), so the runner is **concurrency-safe**. The runner starts the container,
    connects the in-container browser, the orchestrator drives it via `docker exec <c>
    agent-browser …`, and screenshots are `docker cp`-ed out of `/shots` into `out` for the
    canonical judge. See `docker/Dockerfile.steel-ab`.

This runner only builds the prompt, invokes `claude -p`, parses the answer, and writes
`result.json`. Judging + `eval.json` are `core.run`'s job.
"""
import os

from benchmarks.webvoyager import config, tasks
from benchmarks.webvoyager.prompts import agent_prompt, agent_docker_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io
from core.steel import steel_container, collect_screenshots, new_name


def _run_docker(task, *, out, dry):
    """Per-task Steel container path (concurrency-safe); the shared container lifecycle is
    `core.steel`. Collects screenshots so the canonical screenshot judge can score it."""
    if dry:
        cmd = build_claude_cmd(agent_docker_prompt(task, new_name("agentab")),
                               model=config.MODEL or None, max_turns=config.MAX_TURNS)
        print("DRY-RUN command:\n ", preview(cmd))
        return None
    with steel_container("agentab") as cname:
        cmd = build_claude_cmd(
            agent_docker_prompt(task, cname),
            model=config.MODEL or None,
            max_turns=config.MAX_TURNS,
        )
        text = run_claude(cmd, timeout=config.TASK_TIMEOUT)
        answer = extract_answer(text)
        results_io.write_output(out, text)
        collect_screenshots(cname, out)
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "web_name": task.get("web_name"),
        "ques": task["ques"],
        "web": task.get("web"),
        "answer": answer,
    })
    return answer


def run(task, *, env, out, refs=None, dry=False):
    """Run one task; returns the parsed answer (None on --dry-run). Writes result.json."""
    if os.environ.get("WV_AGENT_DOCKER"):
        return _run_docker(task, out=out, dry=dry)

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
