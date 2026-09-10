"""The `claude -p` drive loop, shared by every runner.

A *runner* is a system-under-test (our agent-browser agent, Alumnium, ...). What they all
share is the `claude -p` invocation: build a command, run it with a timeout, pull the
result text out of the JSON envelope, and parse the agent's final `ANSWER:` line. The
browser/MCP specifics live in each benchmark's runner; this module is the generic plumbing
both those runners and `core.judge` build on.
"""
import json
import os
import re
import signal
import subprocess

ANSWER_RE = re.compile(r"^ANSWER:\s*(.*)$", re.MULTILINE)


def extract_answer(text: str) -> str:
    """Pull the last `ANSWER: ...` line out of an agent transcript ("" if none)."""
    matches = ANSWER_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


def build_claude_cmd(prompt, *, model=None, max_turns=None, add_dir=None,
                     mcp_config=None, allowed_tools=None, extra=None, resume=None):
    """Assemble a `claude -p ... --output-format json` argv.

    Covers every runner/judge shape: a plain agent (add_dir), an MCP runner (mcp_config +
    allowed_tools), and the judge (add_dir + low max_turns). Falsy `model` => omit
    `--model` so the CLI uses the subscription default.

    `resume` (a prior call's session_id) continues that session instead of starting a new
    one -- verified live 2026-09-09 to preserve session_id, pinned model, and tool/MCP state
    across the boundary, so a follow-up prompt picks up exactly where the resumed session left
    off (same desktop state, same allowed tools) rather than re-provisioning from scratch.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if add_dir:
        cmd += ["--add-dir", str(add_dir)]
    if mcp_config:
        cmd += ["--mcp-config", str(mcp_config)]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    if extra:
        cmd += list(extra)
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _run_raw(cmd, *, timeout, env=None) -> str:
    """`env` (optional) overrides the child environment — used by the containerized runner to
    hand the orchestrator a PATH where bare `agent-browser` is shadowed, so it can only drive
    the browser via `docker exec` (no host browser). None => inherit the parent environment.
    On timeout, returns whatever stdout was captured rather than raising.

    Runs in its own process group (`start_new_session`) and kills the WHOLE group on timeout,
    not just the direct child. Observed live: `claude -p` with an MCP server (e.g. OSWorld's
    stdio server) spawns that server as a grandchild inheriting the stdout pipe; a plain
    `subprocess.run(..., timeout=...)` only kills the direct child on TimeoutExpired, so the
    orphaned grandchild keeps the pipe open and `communicate()` blocks forever waiting for EOF
    that never comes -- froze an unattended --runs campaign on ONE task for 7+ hours with no
    recovery, well past `timeout`.

    `stdin=DEVNULL` is deliberate, not a default: every batch driver in this repo (including
    g3_full_overnight_driver.sh, the one the G3-full campaign actually ran under) loops
    `while read tid; do ...claude...; done < IDS_FILE` -- the loop body's stdin is the ids
    file, positioned mid-file after the shell's own `read` consumes one line. Without an
    explicit redirect here, `claude -p` inherits that fd; seeing a non-tty stdin, it reads
    whatever's left and folds it into the model's actual input as extra context -- invisible
    in `ps`/argv (the leak isn't a cmd argument), only visible in the captured
    conversation.jsonl transcript. Found live 2026-08-26 re-deriving conversation captures:
    the leaked tail is the rest of that pass's task-id list, sitting right after the real
    TASK instruction. Confirmed on both a fresh capture and one from the original 2026-08-24
    campaign, so this predates today's batch -- an unknown slice of the whole campaign's
    prompts carry a trailing, uninstructed list of sibling task ids. Likely inert (no
    imperative attached) but real contamination; DEVNULL severs it at the source so it can't
    recur regardless of what shell pattern a future driver uses."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        return stdout
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, _ = proc.communicate()   # drain whatever's buffered now that the tree is dead
        return stdout or ""


def run_claude(cmd, *, timeout, env=None) -> str:
    """Run a `claude -p` command and return its result text. Tolerant by design: if the output
    isn't the expected JSON envelope, returns the raw stdout. Never raises on a slow/odd run."""
    raw = _run_raw(cmd, timeout=timeout, env=env)
    try:
        return json.loads(raw).get("result", raw)
    except Exception:
        return raw


def run_claude_meta(cmd, *, timeout, env=None) -> dict:
    """Like `run_claude`, but returns the full JSON envelope instead of just the result text —
    use when a caller needs `num_turns`/`stop_reason`/`is_error` to tell a clean finish (agent
    printed its answer and stopped on its own) from a truncated one (hit --max-turns or errored)
    that happened to score SUCCESS anyway because the desktop state was already correct. `{}` if
    the output isn't the expected JSON envelope."""
    raw = _run_raw(cmd, timeout=timeout, env=env)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def preview(cmd) -> str:
    """Render an argv as a copy-pasteable shell command (for --dry-run)."""
    return " ".join(repr(c) if (" " in c or "\n" in c) else c for c in cmd)
