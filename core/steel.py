"""Per-task Steel browser container: an isolated agent-browser daemon + Chromium per worker.

The host `agent-browser` daemon is a singleton (one `default` session, no isolated-daemon
env), so running the agent_browser harness at concurrency > 1 needs **one container per
task**. This module owns that lifecycle so any benchmark whose runner drives agent-browser
(WebVoyager, BrowseComp, WebArena) becomes concurrency-safe with the same plumbing:

    with steel_container() as cname:
        text = run_claude(build_claude_cmd(prompt_using(cname), ...))
        collect_screenshots(cname, out)   # optional — only screenshot-judged benchmarks

Steel runs Chromium internally and proxies CDP on :9223; we connect agent-browser to it from
inside the container. Image + build: benchmarks/webvoyager/docker/Dockerfile.steel-ab.
"""
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def steel_image():
    return os.environ.get("WV_STEEL_IMAGE", "steel-ab:latest")


def _start(cname):
    """Start one Steel container, wait for its Chromium/CDP, connect agent-browser, and make
    the in-container `/shots` dir. `agent-browser connect 9223` doubles as the readiness probe
    (it succeeds only once Steel's :9223 CDP proxy is live, ~10s cold)."""
    subprocess.run(["docker", "run", "-d", "--name", cname, steel_image()],
                   check=True, timeout=90, capture_output=True)
    for _ in range(90):
        r = subprocess.run(["docker", "exec", cname, "agent-browser", "connect", "9223"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and "Done" in (r.stdout + r.stderr):
            subprocess.run(["docker", "exec", cname, "mkdir", "-p", "/shots"],
                           capture_output=True, timeout=10)
            return
        time.sleep(1)
    raise RuntimeError(f"Steel CDP never came up in container {cname}")


def rm_container(cname):
    try:
        subprocess.run(["docker", "rm", "-f", cname], timeout=30, capture_output=True)
    except Exception:
        pass


@contextmanager
def steel_container(prefix="ab"):
    """Context manager: a started+connected Steel container; cleaned up on exit. Yields the
    container name to embed in the agent prompt (`docker exec <name> agent-browser ...`)."""
    cname = f"{prefix}_" + uuid.uuid4().hex[:12]
    _start(cname)
    try:
        yield cname
    finally:
        rm_container(cname)


def new_name(prefix="ab"):
    """A container name for prompt previews (dry-run) without starting anything."""
    return f"{prefix}_" + uuid.uuid4().hex[:12]


def collect_screenshots(cname, out):
    """docker cp the agent's screenshots out of the container's /shots into `out` (already
    named step_N.png / final.png — what the screenshot judge reads)."""
    Path(out).mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "cp", f"{cname}:/shots/.", str(out)],
                   capture_output=True, timeout=60)
