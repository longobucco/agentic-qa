"""Headless Codex CLI plumbing for benchmark runners.

Codex emits one JSON object per line rather than Claude Code's single JSON envelope.  Keep the
provider-specific parsing here so benchmark runners only consume a small, stable metadata shape.
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from core import procgroups

DISABLED_FEATURES = (
    "shell_tool", "browser_use", "computer_use", "browser_use_external",
    "browser_use_full_cdp_access", "apps", "plugins", "skill_search", "multi_agent",
    "multi_agent_v2", "image_generation", "in_app_browser", "in_app_local_automation",
    "goals", "sleep_tool", "tool_suggest", "view_image", "shell_snapshot", "unified_exec",
)
ALLOWED_MCP_TOOLS = (
    "screenshot", "a11y_tree", "click", "double_click", "right_click", "move", "scroll",
    "type", "key", "wait",
)
APPROVAL_MODE = "dangerously-bypass-approvals-and-sandbox"
# The config key that replaces Codex's own base instructions with a file's text. Found on the
# 0.153.4 binary (`model_instructions_file` in its ConfigToml) and verified live: the session's
# rollout then records base_instructions {text: <file>, provenance: custom} and the model obeys it.
BASE_INSTRUCTIONS_KEY = "model_instructions_file"


def allowed_mcp_tools(zoom_batch, official=False):
    """The Astra toolset: the frozen baseline, plus zoom/batch in that arm -- or, under the
    official protocol, upstream's single `computer` tool (never combined with zoom/batch)."""
    if official:
        return ("computer",)
    return ALLOWED_MCP_TOOLS + (("zoom", "batch") if zoom_batch else ())


def build_codex_cmd(prompt, *, model, cwd, controller_url, reasoning_effort=None,
                    python_bin=None, mcp_extra_env=None, base_instructions=None,
                    extra_config=None):
    """Build an isolated `codex exec` invocation with only the task's OSWorld MCP configured.

    base_instructions replaces Codex's own base instructions: the text is written to a fresh temp
    file passed as BASE_INSTRUCTIONS_KEY (remove it after the run: base_instructions_file(cmd)).
    extra_config is a sequence of TOML `key=value` overrides, each passed as `-c`. With neither,
    the command is exactly the historical one."""
    python_bin = python_bin or sys.executable
    repo_root = str(Path(__file__).resolve().parent.parent)
    # The MCP child starts in ``cwd``; retain access to the benchmark package without relying on
    # the caller having installed this repository as a wheel. Config overrides use TOML syntax,
    # not JSON (their scalar strings happen to be compatible, inline tables are not).
    mcp_env = (
        "{ OSW_CONTROLLER_URL = " + json.dumps(controller_url or "")
        + ", PYTHONPATH = " + json.dumps(repo_root)
        + ", OSW_RESTRICT_RUN_PYTHON = \"1\""
        + "".join(f", {key} = {json.dumps(value)}" for key, value in (mcp_extra_env or {}).items())
        + " }"
    )
    cmd = [
        "codex", "exec", "--json", "--color", "never", "--model", model,
        # Non-interactive MCP calls otherwise fail with "approval policy is never". This is safe
        # here because all host mutation tools are disabled and the only remaining action surface
        # is the benchmark's disposable remote desktop MCP.
        f"--{APPROVAL_MODE}",
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
    ]
    # Astra routes MCP calls through Codex's sandboxed Code Mode host. The host must remain
    # enabled, while every capability other than the injected MCP is disabled.
    for feature in DISABLED_FEATURES:
        cmd += ["--disable", feature]
    cmd += [
        "--cd", str(cwd),
        "-c", f"mcp_servers.osworld.command={json.dumps(str(python_bin))}",
        "-c", "mcp_servers.osworld.args=[\"-m\",\"benchmarks.osworld.mcp.server\"]",
        "-c", f"mcp_servers.osworld.env={mcp_env}",
    ]
    if reasoning_effort:
        cmd += ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
    if base_instructions is not None:
        fd, path = tempfile.mkstemp(prefix="osw-codex-instructions-", suffix=".md")
        with os.fdopen(fd, "w") as fh:
            fh.write(base_instructions)
        cmd += ["-c", f"{BASE_INSTRUCTIONS_KEY}={json.dumps(path)}"]
    for override in extra_config or ():
        cmd += ["-c", override]
    cmd.append(prompt)
    return cmd


def base_instructions_file(cmd):
    """The base-instructions temp file build_codex_cmd wrote for `cmd`, or None."""
    prefix = f"{BASE_INSTRUCTIONS_KEY}="
    for arg in cmd[:-1]:
        if arg.startswith(prefix):
            return json.loads(arg[len(prefix):])
    return None


_NESTED_TOOL_RE = re.compile(r"declare const tools: \{ ([A-Za-z_][A-Za-z0-9_]*)\(")


def offered_tools_from_trace(stderr):
    """Tools the model was offered, from the first `response.created` event Codex logs under
    RUST_LOG=tungstenite::protocol=trace (the server echoes the request's `tools`). Neither the
    `exec --json` stream nor the rollout records them. Names are `<namespace>.<tool>` and, for the
    code-mode host's nested tools declared in its description, `exec.<tool>`. MCP tools are
    deferred by Codex (reachable via the host's ALL_TOOLS, not declared), so they don't appear.
    None when no such event is found (e.g. the transport was not the logged websocket)."""
    for line in (stderr or "").splitlines():
        if '"type":"response.created"' not in line or "{" not in line:
            continue
        try:
            response = json.loads(line[line.index("{"):]).get("response") or {}
        except ValueError:
            continue
        names = []
        for tool in response.get("tools") or []:
            members = tool.get("tools") if tool.get("type") == "namespace" else [tool]
            prefix = f"{tool.get('name')}." if tool.get("type") == "namespace" else ""
            for member in members or []:
                names.append(f"{prefix}{member.get('name')}")
                if member.get("name") == "exec":
                    names += [f"exec.{n}"
                              for n in _NESTED_TOOL_RE.findall(member.get("description") or "")]
        return names
    return None


def _parse_events(raw):
    events, malformed = [], []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            malformed.append(line)
    return events, malformed


def _item(event):
    value = event.get("item")
    return value if isinstance(value, dict) else {}


def _is_tool_item(item):
    """Recognize both current and future Codex tool-like JSONL item types."""
    kind = item.get("type", "")
    return (kind.endswith("_call") or kind in {
        "command_execution", "file_change", "web_search", "image_generation",
    })


def parse_codex_output(raw, *, returncode=0, timed_out=False):
    """Normalize Codex JSONL into the subset required by the benchmark result schema."""
    events, malformed = _parse_events(raw)
    messages = [
        _item(e).get("text", "") for e in events
        if e.get("type") == "item.completed" and _item(e).get("type") == "agent_message"
    ]
    thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    completed = [e for e in events if e.get("type") == "turn.completed"]
    usage = (completed[-1].get("usage") or {}) if completed else {}
    errors = []
    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            errors.append(event.get("message") or event.get("error") or event)
        item = _item(event)
        if (event.get("type") == "item.completed" and item.get("type") == "error"):
            errors.append(item.get("message") or item)
    if malformed:
        errors.append({"malformed_jsonl_lines": len(malformed)})
    tool_items = {}
    idless_completed = []
    idless_started = []
    for event in events:
        item = _item(event)
        phase = event.get("type")
        if phase not in {"item.started", "item.completed"} or not _is_tool_item(item):
            continue
        identity = item.get("id") or item.get("call_id")
        if identity:
            tool_items[identity] = item
        elif phase == "item.completed":
            idless_completed.append(item)
        else:
            idless_started.append(item)
    # Codex normally supplies an id. For schema variants that do not, completed records are the
    # source of truth; count started records only when there are no completions at all (timeout).
    tools = list(tool_items.values()) + (idless_completed or idless_started)
    mcp_tools = [item for item in tools if item.get("type") == "mcp_tool_call"]
    non_mcp_tools = [item for item in tools if item.get("type") != "mcp_tool_call"]
    return {
        "result": messages[-1] if messages else "",
        "session_id": thread,
        "usage": usage,
        "errors": errors or None,
        "is_error": bool(timed_out or returncode or errors),
        "timed_out": timed_out,
        "returncode": returncode,
        "num_turns": len(completed),
        "tool_calls": len(tools),
        "mcp_tool_calls": len(mcp_tools),
        "non_mcp_tool_calls": len(non_mcp_tools),
        "tool_names": [item.get("tool") or item.get("name") or item.get("command")
                       or item.get("type") for item in tools],
        "events": events,
        "raw": raw or "",
    }


SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def session_context(session_id, root=None):
    """What Codex actually applied for a session, read back from its own rollout file.

    The `codex exec --json` stream carries no model field at all (verified on the accepted
    canary: 27 events, not one names a model), so `--model gpt-6-astra` is a request with no
    receipt. The rollout Codex writes under ~/.codex/sessions does name it, along with the
    reasoning effort and the approval/sandbox policy actually in force.

    This exists because the Sonnet campaign ran across three different models for weeks with
    nothing on disk recording it (benchmarks/osworld/config.py::MODEL). A pinned model that is
    never verified is a claim, not a control.

    Returns {} when the rollout can't be found or parsed -- callers record None rather than
    guessing, and a run is never failed over provenance plumbing.
    """
    if not session_id:
        return {}
    root = Path(root) if root else SESSIONS_ROOT
    try:
        matches = sorted(root.rglob(f"rollout-*-{session_id}.jsonl"))
    except OSError:
        return {}
    if not matches:
        return {}
    meta, turn = {}, {}
    try:
        with matches[-1].open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind, payload = record.get("type"), record.get("payload") or {}
                if kind == "session_meta" and not meta:
                    meta = payload
                elif kind == "turn_context" and not turn:
                    turn = payload
                if meta and turn:
                    break
    except OSError:
        return {}
    sandbox = turn.get("sandbox_policy")
    profile = turn.get("permission_profile")
    return {
        "model_served": turn.get("model"),
        "reasoning_effort_served": turn.get("effort")
        or ((turn.get("collaboration_mode") or {}).get("settings") or {}).get("reasoning_effort"),
        "cli_version_served": meta.get("cli_version"),
        "model_provider": meta.get("model_provider"),
        "approval_policy_served": turn.get("approval_policy"),
        "sandbox_policy_served": sandbox.get("type") if isinstance(sandbox, dict) else sandbox,
        "permission_profile_served": (profile.get("type") if isinstance(profile, dict)
                                      else profile),
        "rollout_path": str(matches[-1]),
    }


def run_codex_meta(cmd, *, timeout, env=None):
    """Run Codex in its own process group and retain partial JSONL on timeout.

    Registers the group with `core.procgroups` for the duration of the call (unregistered in a
    `finally`) so an interrupted `core.run.main()` can reap it even if this call never reaches
    its own timeout path -- see core/procgroups.py for why. If the harness is already
    interrupted when called, raises `procgroups.Interrupted` without starting Codex at all; if
    the harness is interrupted WHILE this call is in flight (its group killed by the signal
    handler, not by this function's own timeout), raises `procgroups.Interrupted` instead of
    returning the partial JSONL as a normal result -- callers must never let a killed-by-
    interrupt run get parsed and scored like a real (or even a timed-out) one."""
    if procgroups.is_interrupted():
        raise procgroups.Interrupted("harness interrupted before Codex could start")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    pgid = proc.pid   # == the new process group's id under start_new_session=True (setsid);
                        # os.getpgid(proc.pid) can raise ProcessLookupError for a child that
                        # already exited by the time we ask, which proc.pid never can.
    procgroups.register(pgid)
    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        if procgroups.is_interrupted():
            raise procgroups.Interrupted(
                "Codex process group was reaped by a harness interrupt, not its own timeout")
    finally:
        procgroups.unregister(pgid)
    meta = parse_codex_output(stdout, returncode=proc.returncode or 0, timed_out=timed_out)
    meta["stderr"] = stderr or ""
    return meta
