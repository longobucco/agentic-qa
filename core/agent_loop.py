"""The `claude -p` drive loop, shared by every runner.

A *runner* is a system-under-test (our agent-browser agent, Alumnium, ...). What they all
share is the `claude -p` invocation: build a command, run it with a timeout, pull the
result text out of the JSON envelope, and parse the agent's final `ANSWER:` line. The
browser/MCP specifics live in each benchmark's runner; this module is the generic plumbing
both those runners and `core.judge` build on.
"""
import json
import re
import subprocess

ANSWER_RE = re.compile(r"^ANSWER:\s*(.*)$", re.MULTILINE)


def extract_answer(text: str) -> str:
    """Pull the last `ANSWER: ...` line out of an agent transcript ("" if none)."""
    matches = ANSWER_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


def build_claude_cmd(prompt, *, model=None, max_turns=None, add_dir=None,
                     mcp_config=None, allowed_tools=None, extra=None):
    """Assemble a `claude -p ... --output-format json` argv.

    Covers every runner/judge shape: a plain agent (add_dir), an MCP runner (mcp_config +
    allowed_tools), and the judge (add_dir + low max_turns). Falsy `model` => omit
    `--model` so the CLI uses the subscription default.
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
    return cmd


def run_claude(cmd, *, timeout) -> str:
    """Run a `claude -p` command and return its result text.

    Tolerant by design: on timeout, return whatever stdout was captured; if the output is
    not the expected JSON envelope, return the raw stdout. Never raises on a slow/odd run.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        raw = proc.stdout
    except subprocess.TimeoutExpired as e:
        raw = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    try:
        return json.loads(raw).get("result", raw)
    except Exception:
        return raw


def preview(cmd) -> str:
    """Render an argv as a copy-pasteable shell command (for --dry-run)."""
    return " ".join(repr(c) if (" " in c or "\n" in c) else c for c in cmd)
