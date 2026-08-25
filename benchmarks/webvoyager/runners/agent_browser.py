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
import functools
import os
import subprocess
import tempfile

from benchmarks.webvoyager import config, tasks
from benchmarks.webvoyager.prompts import agent_prompt, agent_docker_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude
from core import results as results_io
from core.steel import steel_container, collect_screenshots, new_name


@functools.lru_cache(maxsize=1)
def _hostless_env():
    """Environment for the orchestrator in Docker mode that makes a HOST browser impossible.

    The browser is supposed to live only inside the Steel container, driven via `docker exec
    <cname> agent-browser ...`. But the orchestrator `claude -p` runs on the host with shell
    access, and a stray *bare* `agent-browser` call would make the host daemon launch a host
    Chrome — violating the no-local-env requirement. So we prepend a PATH dir holding an
    `agent-browser` stub that fails fast: bare calls die with code 127 (the agent then uses
    `docker exec` as instructed), while `docker exec ... agent-browser` still hits the real
    in-container binary. The harness's own browser calls already go through `docker exec`."""
    d = tempfile.mkdtemp(prefix="wv-hostless-")
    stub = os.path.join(d, "agent-browser")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\n"
                "echo 'host agent-browser is DISABLED in WV_AGENT_DOCKER mode; drive the "
                "browser with: docker exec <container> agent-browser ...' >&2\n"
                "exit 127\n")
    os.chmod(stub, 0o755)
    return {**os.environ, "PATH": d + os.pathsep + os.environ.get("PATH", "")}


def _capture_final_screenshot(port, out):
    """Guarantee >=1 screenshot for the (screenshot-only) judge, independent of the agent.

    The judge auto-fails any run with no images, and the agent frequently finishes (or hits
    the task timeout) without saving one. Since the harness owns the browser, after the agent
    exits we re-attach agent-browser to the SAME CDP port and snap `final.png` of the end
    state ourselves. Best-effort: a capture failure must never fail the task."""
    dest = out / "final.png"
    try:
        subprocess.run(["agent-browser", "connect", str(port)],
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["agent-browser", "screenshot", str(dest)],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        pass
    return dest.exists()


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
        text = run_claude(cmd, timeout=config.TASK_TIMEOUT, env=_hostless_env())
        answer = extract_answer(text)
        results_io.write_output(out, text)
        # Guarantee >=1 screenshot for the screenshot-only judge, same as the native path:
        # snap the container's end state ourselves rather than trust the agent to have done it.
        # Best-effort: a slow/failed capture must NOT error the task (the agent's own step
        # screenshots may still exist, and collect_screenshots runs regardless).
        try:
            subprocess.run(["docker", "exec", cname, "agent-browser", "screenshot",
                            "/shots/final.png"], capture_output=True, text=True, timeout=45)
        except Exception:
            pass
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
    _capture_final_screenshot(env.port, out)   # guarantee the judge has >=1 screenshot
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "web_name": task.get("web_name"),
        "ques": task["ques"],
        "web": task.get("web"),
        "answer": answer,
    })
    return answer
