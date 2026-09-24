"""Shared, policy-free primitives extracted from runners/agent_computer.py so another runner
can reuse the exact same MCP config shape, telemetry, provenance, post-run watchdog, transcript
capture, and scoring logic without importing agent_computer's own orchestration or G5-arm policy
knobs.

Extraction discipline (see the plan's own instruction, Section 6): only primitives whose
behavior is already pinned by benchmarks/osworld/tests/test_runner.py's characterization tests
moved here, verbatim. Nothing in this module reads a G5-arm-specific config knob
(ENFORCE_SANDBOX, RESTRICT_RUN_PYTHON, INLOOP_VERIFY) -- those stay in the runner that owns that
policy. agent_computer.py re-imports every name below so existing external imports of
`benchmarks.osworld.runners.agent_computer._mcp_config` etc. keep resolving unchanged.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import requests

from benchmarks.osworld import config, evaluate, tasks
from benchmarks.osworld.env import osworld_eval
from core import procgroups

# The checkout this harness runs from (benchmarks/osworld/runners/ -> repo root).
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]

OSWORLD_TOOLS = [
    "mcp__osworld__screenshot", "mcp__osworld__a11y_tree",
    "mcp__osworld__click", "mcp__osworld__double_click", "mcp__osworld__right_click",
    "mcp__osworld__move", "mcp__osworld__scroll", "mcp__osworld__type",
    "mcp__osworld__key", "mcp__osworld__run_python", "mcp__osworld__wait",
]


# Written by the official MCP server into the run's out dir (OSW_MCP_STATE_FILE): proof it
# started, plus the steps it counted (mcp/official_computer.write_state).
MCP_STATE_FILE = "mcp_state.json"


def mcp_child_env(out_dir=None):
    """Protocol env the MCP server child must see. Both CLIs pass only what they are given, so
    the server can't read these from the runner's environment. Under the official protocol,
    `out_dir` (the run's output dir) also names the server's liveness/step state file."""
    env = {}
    if config.OFFICIAL:
        env.update(OSW_PROTOCOL="official", OSW_MAX_STEPS=str(config.MAX_STEPS),
                   OSW_SLEEP_AFTER_EXECUTION=str(config.SLEEP_AFTER_EXECUTION),
                   OSW_SCREEN_WIDTH=str(config.SCREEN_WIDTH),
                   OSW_SCREEN_HEIGHT=str(config.SCREEN_HEIGHT))
        if out_dir is not None:
            env["OSW_MCP_STATE_FILE"] = str(Path(out_dir) / MCP_STATE_FILE)
    return env


def reset_mcp_state(out_dir):
    """Remove a state file left by an earlier attempt at this run dir, before the agent starts:
    only the file this run's server writes may count as proof it started."""
    (Path(out_dir) / MCP_STATE_FILE).unlink(missing_ok=True)


def read_mcp_state(out_dir):
    """The official MCP server's state for this run ({"started", "steps_used", "max_steps"}),
    or None when it never wrote one (it didn't start, or crashed before startup finished)."""
    try:
        state = json.loads((Path(out_dir) / MCP_STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("started") else None


def mcp_unavailable_infra_rec(task):
    """The agent ran but the official MCP server never reported starting: the agent had no
    `computer` tool, so the run measures the harness, not the model -- never scored, retried."""
    return {"id": task["id"], "outcome": "HARNESS_ERROR", "error_type": "McpServerUnavailable",
            "error": f"official MCP server wrote no {MCP_STATE_FILE} (it never started)",
            "at": datetime.now(timezone.utc).isoformat()}


def _mcp_config(controller_url, out_dir=None):
    spec = {
        "type": "stdio", "command": "python",
        "args": ["-m", "benchmarks.osworld.mcp.server"],
        "env": {"OSW_CONTROLLER_URL": controller_url or "", **mcp_child_env(out_dir)},
    }
    if config.OFFICIAL:
        # The official protocol starts the CLI (and so this child) in an empty temp dir, not
        # the repo: keep the benchmark package importable, as core/codex_loop.py does for Codex,
        # and run it with this interpreter (same as Codex), not whatever `python` is on PATH.
        spec["command"] = sys.executable
        spec["env"]["PYTHONPATH"] = str(CHECKOUT_ROOT)
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mcpServers": {"osworld": spec}}, f)
    f.close()
    return f.name


def _action_history(answer):
    # OSWorld marks infeasible tasks by a final FAIL action; map our answer onto that
    a = (answer or "").strip().upper()
    if a.startswith("FAIL") or "INFEASIBLE" in a:
        return ["FAIL"]
    return [answer]


# Observed live (2026-08-13): the claude subprocess itself is bounded by TASK_TIMEOUT and exits
# cleanly, but the POST-agent work -- eval-state capture (screenshot/file/command against the
# same flaky Daytona-proxied controller) and official scoring -- has no bound of its own. A run
# that reported ~950s of actual agent activity took 3600s+ wall-clock end to end; the gap sat in
# this unbounded tail, not in provisioning (see sandbox._PROVISION_TIMEOUT_S, a separate bound).
# Same watchdog pattern as provisioning: run in a worker thread, cap wall-clock, raise a plain
# RuntimeError past the cap so core.run's work() files it as INFRA_FLAKE and a later invocation
# retries the run untouched -- a hung capture/score no longer strands a whole unit for an hour.
_POST_RUN_TIMEOUT_S = int(os.environ.get("OSW_POST_RUN_TIMEOUT", "600"))


def _bounded(label, fn, *args):
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args)
        try:
            return fut.result(timeout=_POST_RUN_TIMEOUT_S)
        except FutureTimeoutError:
            raise RuntimeError(
                f"{label} exceeded {_POST_RUN_TIMEOUT_S}s -- treated as a hang, not a "
                f"legitimate wait"
            ) from None


def _capture_eval_state(ctrl, task, out):
    # for the offline fallback + an audit screenshot
    ev = task.get("evaluator", {}) or {}
    result_spec = ev.get("result")
    captured = None
    try:
        if isinstance(result_spec, dict):
            rtype = result_spec.get("type")
            if rtype in ("vm_command_line", "command"):
                captured = ctrl.execute(result_spec.get("command", ""))
            elif rtype in ("vm_file", "file"):
                data = ctrl.read_file(result_spec.get("path", ""))
                dest = out / (result_spec.get("dest") or "eval_artifact")
                dest.write_bytes(data)
                try:
                    captured = data.decode()
                except Exception:
                    captured = f"<binary:{len(data)} bytes at {dest.name}>"
    except Exception as e:
        print(f"[osworld] eval-state capture failed: {e}")
    try:
        (out / "final.png").write_bytes(ctrl.screenshot())
    except Exception:
        pass
    return captured


def _agent_telemetry(meta):
    """Everything from the `claude -p` JSON envelope a later analysis might need, captured
    once so a costly run never has to be repeated for a forgotten field. Was previously
    discarded."""
    usage = meta.get("usage") or {}
    return {
        "agent_clean_finish": None,   # filled by the caller (needs the parsed answer)
        "agent_num_turns": meta.get("num_turns"),
        "agent_stop_reason": meta.get("stop_reason"),
        "agent_terminal_reason": meta.get("terminal_reason"),
        "agent_subtype": meta.get("subtype"),
        "agent_is_error": meta.get("is_error"),
        "agent_errors": meta.get("errors"),
        "agent_api_error_status": meta.get("api_error_status"),
        "agent_permission_denials": meta.get("permission_denials"),
        # cost + tokens: the only basis for a real (not hand-waved) budget for --runs N
        "agent_cost_usd": meta.get("total_cost_usd"),
        "agent_input_tokens": usage.get("input_tokens"),
        "agent_output_tokens": usage.get("output_tokens"),
        "agent_cache_read_tokens": usage.get("cache_read_input_tokens"),
        "agent_cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "agent_model_usage": meta.get("modelUsage"),
        # wall-clock: separates "slow agent" from "slow API" when a run looks like an outlier
        "agent_duration_ms": meta.get("duration_ms"),
        "agent_duration_api_ms": meta.get("duration_api_ms"),
        # provenance: lets a specific run be traced back to its CLI session
        "agent_session_id": meta.get("session_id"),
        "agent_uuid": meta.get("uuid"),
    }


def _evaluator_provenance():
    """Never let a provenance record fail a run: scoring may be unavailable entirely."""
    try:
        from benchmarks.osworld.env.osworld_eval import evaluator_provenance
        return evaluator_provenance()
    except Exception:
        return {"evaluator_commit": None, "evaluator_package": None}


def _provenance(task, ctrl, started_at):
    """Per-run pinning record. harness.json gets overwritten by the next invocation; this
    rides with the individual run so the record stays self-describing after reconfiguration."""
    return {
        "task_sha256": hashlib.sha256(
            json.dumps(task, sort_keys=True).encode()).hexdigest(),
        "image": config.KVM_IMAGE if config.BACKEND == "kvm" else config.IMAGE,
        # The model we ASKED for. Empty means no --model was passed and the CLI picked its own
        # default -- the gap that let the G3 campaign run across three different models without
        # anything on disk recording it (see config.MODEL). What actually served the request is
        # cross-checked separately, in _model_mismatch.
        "model_requested": config.MODEL or None,
        "controller_url": getattr(ctrl, "base_url", None) or config.CONTROLLER_URL or None,
        "release": config.RELEASE,
        # Who computed the verdict. The task set and the guest image were pinned long before
        # the evaluator library was (see data/download_evaluators.py), so a pass rate is only
        # comparable against another one carrying the same value here.
        **_evaluator_provenance(),
        "max_turns": config.MAX_TURNS,
        "task_timeout": config.TASK_TIMEOUT,
        "observation": config.OBSERVATION,
        "action_space": config.ACTION_SPACE,
        "effort": config.EFFORT or None,
        "max_output_tokens": config.MAX_OUTPUT_TOKENS,
        "max_steps": config.MAX_STEPS,
        "screen_size": f"{config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}",
        "protocol": config.PROTOCOL or None,
        "backend": config.BACKEND,
        "kvm_image": config.KVM_IMAGE if config.BACKEND == "kvm" else None,
        "kvm_qcow2_sha256": config.KVM_QCOW2_SHA256 or None,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def claude_env():
    """Child environment for `claude -p`: the parent's, plus the output-token limit when the
    protocol sets one and, under the official protocol, the auto-updater off (the CLI must stay
    at config.CLAUDE_CODE_VERSION for the whole campaign). None keeps the historical behavior
    (inherit unchanged)."""
    extra = {}
    if config.MAX_OUTPUT_TOKENS:
        extra["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(config.MAX_OUTPUT_TOKENS)
    if config.OFFICIAL:
        extra["DISABLE_AUTOUPDATER"] = "1"
    return {**os.environ, **extra} if extra else None


_CLAUDE_CLI_VERSION = {}


def claude_cli_version(*, cached=True):
    """`claude --version` (e.g. "2.1.280" from "2.1.280 (Claude Code)"), run with the same env
    as a real run; None when the CLI can't be run or says nothing. Cached per process so every
    run's provenance records it without a subprocess each time; the preflight asks fresh."""
    if cached and "v" in _CLAUDE_CLI_VERSION:
        return _CLAUDE_CLI_VERSION["v"]
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                             timeout=30, check=True, env=claude_env()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    version = out.split()[0] if out else None
    _CLAUDE_CLI_VERSION["v"] = version
    return version


def official_probe_already_passed():
    """True when the campaign driver already ran the live-model isolation probe for THIS driver
    run (it exports OSW_OFFICIAL_PREFLIGHT_OK=<its run id> next to OSW_KVM_DRIVER_RUN): children
    then skip only that probe, never the cheap checks."""
    ok = os.environ.get("OSW_OFFICIAL_PREFLIGHT_OK", "").strip()
    return bool(ok) and ok == os.environ.get("OSW_KVM_DRIVER_RUN", "").strip()


def protocol_wait(seconds, *, sleep=None):
    """Upstream's fixed settle sleeps (after setup, before evaluate) -- official protocol only;
    a no-op otherwise, so the legacy harness keeps its timings.

    Interruptible by default: waits on `core.procgroups`' interrupted flag (via
    `wait_interrupted`) rather than blocking blindly, so a harness SIGTERM/SIGINT during this
    wait is noticed AT ONCE (raises `procgroups.Interrupted`) instead of only at the next
    spawner call -- up to POST_SETUP_WAIT_S/PRE_EVAL_WAIT_S (60s/20s) later, long enough that
    core.run's `ex.shutdown(wait=True, ...)` could still be blocked when the campaign driver's
    own grace period SIGKILLs the whole process, losing the INTERRUPTED infra record entirely
    (task-10b fix round 2). `sleep` (test injection) replaces the wait mechanism outright and
    is never interrupted -- existing tests use it to observe the call without a real delay."""
    if not config.OFFICIAL:
        return
    if sleep is not None:
        sleep(seconds)
        return
    if procgroups.wait_interrupted(seconds):
        raise procgroups.Interrupted("harness interrupted during protocol_wait")


def _claude_assistant_blocks(path):
    """Content blocks of every assistant message in a Claude Code session JSONL, in order.
    Unparseable lines are skipped; a string `content` is yielded as one text block."""
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, str):
            yield {"type": "text", "text": content}
            continue
        for block in content or []:
            if isinstance(block, dict):
                yield block


def claude_transcript_actions(path):
    """(assistant text blocks, `computer` tool_use inputs) from a Claude Code session JSONL, in
    order -- what official_protocol.final_action needs to apply upstream's termination rule."""
    texts, calls = [], []
    for block in _claude_assistant_blocks(path):
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif block.get("type") == "tool_use" and block.get("name") == "mcp__osworld__computer":
            calls.append(block.get("input") or {})
    return texts, calls


def _host_path_markers():
    """Paths whose appearance in a session means host/repo context reached the agent: this
    checkout, the main repo when it is a git worktree, and the harness's own cwd (where the CLI
    would otherwise have started). Home and / are never markers (too broad)."""
    marks = {str(CHECKOUT_ROOT), os.getcwd()}
    if CHECKOUT_ROOT.parent.name == ".worktrees":
        marks.add(str(CHECKOUT_ROOT.parent.parent))
    return sorted(m for m in marks if m not in ("/", str(Path.home())))


def claude_transcript_context_leaks(path):
    """Host context kinds the CLI attached to a Claude Code session (sorted, [] when clean):
    hook_context (SessionStart/plugin hook output), memory (auto-memory or CLAUDE.md files),
    user_email and git_status (session_context), repo_path (the checkout or harness cwd, e.g.
    in the environment block). Upstream's agent sees only its system prompt and the task."""
    raw = Path(path).read_text(errors="replace")
    leaks = set()
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "attachment":
            continue
        att = ev.get("attachment") or {}
        kind = att.get("type") or ""
        # Claude Code's DEFAULT system prompt explains the MEMORY.md convention; only a
        # memory file actually attached counts, not the system-prompt snapshot mentioning it.
        if kind != "prompt_snapshot" and "MEMORY.md" in line:
            leaks.add("memory")
        if kind.startswith("hook_"):
            leaks.add("hook_context")
        elif kind == "instructions":   # CLAUDE.md / auto-memory files
            leaks.add("memory")
        elif kind == "session_context":
            ctx = att.get("context") or {}
            if ctx.get("userEmail"):
                leaks.add("user_email")
            if ctx.get("gitStatus"):
                leaks.add("git_status")
        elif kind == "environment" and (att.get("snapshot") or {}).get("isGitRepo"):
            leaks.add("git_status")
    if any(m in raw for m in _host_path_markers()):
        leaks.add("repo_path")
    return sorted(leaks)


def claude_transcript_offered_tools(path):
    """Names of the tools the model was actually offered, from the session's last
    system-prompt snapshot that lists tools (None if there is none). Authoritative where the
    stream-json init event is not: on CLI 2.1.280 init omitted Glob/Grep/MCP-resource tools
    that the model was nonetheless given."""
    offered = None
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        att = ev.get("attachment") if isinstance(ev, dict) else None
        if isinstance(att, dict) and att.get("type") == "prompt_snapshot" and "tools" in att:
            offered = [t.get("name") for t in att.get("tools") or [] if isinstance(t, dict)]
    return offered


def claude_session_file(session_id):
    """Claude Code's own transcript for a session (same lookup as
    _save_conversation_transcript), or None."""
    if not session_id:
        return None
    matches = list(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
    return matches[0] if matches else None


def claude_project_dir_name(cwd):
    """The ~/.claude/projects/<name> Claude Code files a session under for `cwd`: every
    non-alphanumeric character becomes '-' (verified on disk, CLI 2.1.280: .../T/osw_claude__x
    -> -private-var-...-T-osw-claude--x)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def claude_workdir_session_file(workdir):
    """The newest session transcript Claude Code wrote for a run started in `workdir`, or None.
    For a run whose envelope carries no session_id (e.g. killed at TASK_TIMEOUT): the official
    run's cwd is a fresh temp dir, so its project dir holds only that run's session. Matched on
    the encoded basename (the full path may be realpath'd, e.g. /var -> /private/var)."""
    if not workdir:
        return None
    name = claude_project_dir_name(os.path.basename(str(workdir).rstrip("/")))
    try:
        matches = list(Path.home().glob(f".claude/projects/*{name}/*.jsonl"))
    except OSError:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def claude_transcript_tool_names(path):
    """Name of every tool_use in a Claude Code session JSONL, in order (the per-run audit that
    the official protocol's computer-only tool set actually held)."""
    return [b.get("name") for b in _claude_assistant_blocks(path) if b.get("type") == "tool_use"]


# Ubuntu's accessibility tree wraps geometry in a namespaced attribute (see the guest's
# /accessibility route); these are the only pieces of the pure-logic accessibility parsing
# _a11y_health needs -- just enough to count nodes with usable screen geometry, not the full
# target-resolution machinery.
_A11Y_NS_COMPONENT = "https://accessibility.ubuntu.example.org/ns/component"
_A11Y_COORD_RE = re.compile(r"-?\d+")


def _a11y_pair(raw):
    """Parse the tree's "(x, y)" / "(w, h)" attribute form."""
    if not raw:
        return None
    nums = _A11Y_COORD_RE.findall(raw)
    if len(nums) < 2:
        return None
    return int(nums[0]), int(nums[1])


def _a11y_unwrap_tree(raw):
    """The guest's /accessibility route does NOT return raw XML: it returns a JSON object
    `{"AT": "<desktop-frame .../>"}`. Accepts either shape (and tolerates the extra
    `{"result": ...}` layer the MCP transport adds when a tool result is logged)."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text.startswith("{"):
        return text
    for _ in range(2):          # at most {"result": "{\"AT\": ...}"}
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return text
        if not isinstance(obj, dict):
            return text
        nxt = obj.get("AT") if "AT" in obj else obj.get("result")
        if not isinstance(nxt, str):
            return text
        text = nxt.strip()
        if text.startswith("<"):
            return text
    return text


def _a11y_tree_health(raw):
    """Is the accessibility channel actually reporting? -> {"nodes", "elements", "ok", "reason"}.

    `nodes` counts child elements of the root, `elements` those with usable geometry. `ok` is
    False for the three distinguishable failures -- unreachable/blank, unparseable, and the
    root-only tree that this harness produced on all 456 captures before the AT-SPI bus was added
    to docker/start.sh.
    """
    xml = _a11y_unwrap_tree(raw)
    if not xml.strip():
        return {"nodes": 0, "elements": 0, "ok": False, "reason": "empty response"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return {"nodes": 0, "elements": 0, "ok": False, "reason": f"unparseable: {e}"}
    nodes = sum(1 for _ in root.iter()) - 1
    if nodes <= 0:
        return {"nodes": 0, "elements": 0, "ok": False,
                "reason": "root node only -- the AT-SPI bridge is not reporting this desktop"}
    elements = 0
    for node in root.iter():
        pos = _a11y_pair(node.get(f"{{{_A11Y_NS_COMPONENT}}}screencoord"))
        size = _a11y_pair(node.get(f"{{{_A11Y_NS_COMPONENT}}}size"))
        if not pos or not size:
            continue
        x, y = pos
        w, h = size
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            continue
        elements += 1
    return {"nodes": nodes, "elements": elements, "ok": elements > 0,
            "reason": None if elements else
                      f"{nodes} node(s) but none with usable geometry"}


def _a11y_health(ctrl):
    """One /accessibility probe per run, recorded in result.json as `a11y_*`.

    An environment fact, not an arm knob, which is why it lives here: the guest returned an empty
    accessibility tree on all 456 captures taken across every campaign before docker/start.sh was
    given a D-Bus session bus and the AT-SPI bridges, and nothing on disk recorded that -- the
    channel looked merely unpopular (0.8% of tool calls) rather than broken. Recording it makes the
    repair verifiable from the results tree instead of trusted, and makes a future regression
    visible on the first run rather than after a thousand.

    Never raises and never blocks: a dead or slow controller yields ok=False, same as a dead bridge,
    with `reason` telling the two apart.
    """
    if ctrl is None:
        return {"a11y_ok": None, "a11y_nodes": None, "a11y_reason": "no controller"}
    try:
        health = _bounded("a11y probe", lambda: _a11y_tree_health(ctrl.a11y_tree()))
    except Exception as e:
        return {"a11y_ok": False, "a11y_nodes": 0, "a11y_reason": f"{type(e).__name__}: {e}"}
    # _bounded raises RuntimeError on timeout, so the except above is the timeout path too --
    # tree_health itself always returns a dict.
    return {"a11y_ok": health["ok"], "a11y_nodes": health["nodes"],
            "a11y_elements": health["elements"], "a11y_reason": health["reason"]}


def _served_by(meta):
    """Which model actually answered, per the CLI's own modelUsage. Haiku shows up in nearly
    every session as Claude Code's internal auxiliary model (a few hundred tokens for things
    like titling); it never drives the agent, so it is not the answer to this question."""
    served = [m for m in (meta.get("modelUsage") or {}) if not m.startswith("claude-haiku")]
    return sorted(served)


def _model_mismatch(meta):
    """Flag a run the CLI did NOT serve with the model we pinned, so it can be excluded rather
    than silently averaged in. Recorded per run because this failed silently for a whole
    campaign: with no --model passed and nothing on disk naming the model, three models' runs
    sat indistinguishable in one results tree until modelUsage was audited weeks later."""
    served = _served_by(meta)
    if not config.MODEL:
        # Unpinned: not a mismatch (nothing was promised), but still worth naming explicitly.
        return {"model_served": served or None, "model_pinned": False, "model_mismatch": None}
    mismatch = served != [config.MODEL]
    if mismatch:
        print(f"[osworld] WARNING model mismatch: pinned {config.MODEL!r} but the CLI reports "
              f"{served or 'nothing'} -- this run is NOT comparable to the pinned campaign")
    return {"model_served": served or None, "model_pinned": True, "model_mismatch": mismatch}


def _clean_finish(meta, answer):
    """Did the agent finish on its own (printed ANSWER) or get cut off (max-turns/error)? A
    SUCCESS without a clean finish means the desktop state already happened to satisfy the
    evaluator -- incidental, not evidence the agent completed the task."""
    return bool(answer) and not meta.get("is_error", False)


def _annotate_incidental(rec, clean_finish):
    """Flag a SUCCESS the agent didn't earn cleanly. Leaves FAILURE and clean SUCCESS untouched."""
    if rec.get("verdict") == "SUCCESS" and not clean_finish:
        rec = {**rec, "note": "incidental: agent did not finish cleanly (no ANSWER / hit "
                              "max-turns or errored) — the state happened to already satisfy "
                              "the evaluator, not necessarily evidence the agent completed the "
                              "task"}
    return rec


_EVAL_RETRY_ATTEMPTS = 4
_EVAL_RETRY_BASE_DELAY_S = 5

# Transient scoring failures worth another attempt: all of these are the Daytona proxy
# misbehaving under load right after a long agent session, never a real verdict.
#   - ConnectionError: bare ConnectionResetError(54, ...) from the proxy. Observed often
#     (>1/3 of scoring attempts in the first G3 batch); this was the original reason for
#     retrying at all.
#   - JSONDecodeError: the single largest cause of lost runs in the whole G3 campaign --
#     96 of 150 EVAL_ERROR, across 33 tasks (inventory 2026-09-04). It surfaces because
#     upstream's PythonController does `if response.status_code == 200: return
#     response.json()`, so a 200 carrying an empty or HTML body raises straight out of
#     desktop_env instead of being retried in there. Catching json.JSONDecodeError covers
#     requests.exceptions.JSONDecodeError too -- it subclasses it (verified on requests
#     2.34.2), so listing only the stdlib one is deliberate, not an oversight.
#   - Timeout / ChunkedEncodingError: same proxy, cut mid-response.
_TRANSIENT_EVAL_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    json.JSONDecodeError,
)


def _evaluate_with_retry(url, task, action_history, cache_dir):
    """Retry the official evaluator on a transient proxy failure (_TRANSIENT_EVAL_ERRORS).

    Backoff is exponential (5s, 15s, 45s) rather than the flat 5s this used to use: the
    proxy needs time to recover after a long session, and a fixed short gap just retried
    back into the same bad window. Worst case adds 65s, well inside _POST_RUN_TIMEOUT_S.

    Repeating the scoring attempt is safe: the evaluator's postconfig steps are idempotent
    and its getters only READ desktop state, so a retry re-reads the same desktop rather
    than mutating what it is about to grade.
    """
    last_err = None
    for attempt in range(_EVAL_RETRY_ATTEMPTS):
        try:
            return osworld_eval.evaluate_official(url, task, action_history, cache_dir=cache_dir)
        except _TRANSIENT_EVAL_ERRORS as e:
            last_err = e
            if attempt < _EVAL_RETRY_ATTEMPTS - 1:
                time.sleep(_EVAL_RETRY_BASE_DELAY_S * (3 ** attempt))
    raise last_err


# Task 11c: bound how much of the scoring cache dir eval_artifacts keeps, so a stray huge
# download never blows up a run dir. Module-level (not local constants) so a test can lower
# them to exercise the cap logic without writing gigabytes of fixture data.
_EVAL_ARTIFACT_MAX_FILE_BYTES = 50 * 1024 * 1024      # single-file cap
_EVAL_ARTIFACT_MAX_TOTAL_BYTES = 200 * 1024 * 1024    # per-run cap across all kept files


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_eval_artifacts(cache_dir, out, task):
    """Copy the files the official getters actually placed in `cache_dir` into
    <out>/eval_artifacts/ -- the real state the verdict was computed from. Both the agent's
    result files (get_vm_file etc) and the expected/gold files (get_cloud_file etc) land in
    this one dir (see osworld_eval.hash_gold_artifacts, which already narrows to just the gold
    names for hashing); this keeps both, since a failed run is usually only diagnosable by
    comparing the two. Deliberately NOT the same thing as _capture_eval_state, which runs
    BEFORE the evaluator's postconfig (document save, archive unzip, ...) and so often
    captures stale state -- this runs right after scoring, whatever the verdict, and right
    before the caller removes cache_dir (found live analysing the 2026-09-24 canary: two failed
    runs were undiagnosable because neither captured what the evaluator actually compared).

    Bounded per _EVAL_ARTIFACT_MAX_FILE_BYTES / _EVAL_ARTIFACT_MAX_TOTAL_BYTES: a file over
    either cap is listed (name/size/sha256) but not copied (kept: False) -- still identifiable
    against a fresh download even though the bytes themselves weren't kept.

    Diagnostics-only: never raises. An unreadable file or a failed copy is recorded in the
    returned manifest instead -- this must never change a verdict or crash an otherwise-good
    run.
    """
    manifest = []
    cache = Path(cache_dir)
    if out is None or not cache.is_dir():   # nowhere to keep them / nothing was downloaded
        return manifest
    gold_names = set(osworld_eval.gold_dest_filenames(task))
    dest_root = out / "eval_artifacts"
    total = 0
    try:
        paths = sorted(p for p in cache.rglob("*") if p.is_file())
    except OSError as e:
        return [{"error": f"{type(e).__name__}: {e}"}]
    for path in paths:
        name = path.relative_to(cache).as_posix()
        entry = {"name": name, "kept": False}
        if name in gold_names:
            entry["is_gold"] = True
        try:
            size = path.stat().st_size
            entry["size"] = size
            entry["sha256"] = _sha256_file(path)
        except OSError as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            manifest.append(entry)
            continue
        oversize = (size > _EVAL_ARTIFACT_MAX_FILE_BYTES
                    or total + size > _EVAL_ARTIFACT_MAX_TOTAL_BYTES)
        if not oversize:
            try:
                dest = dest_root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                entry["kept"] = True
                total += size
            except OSError as e:
                entry["error"] = f"{type(e).__name__}: {e}"
        manifest.append(entry)
    return manifest


def _score(ctrl, task, answer, out):
    url = ctrl.base_url if ctrl else config.CONTROLLER_URL
    reward, err = None, None
    # own the cache dir so the downloaded gold can be hashed afterwards
    # (see osworld_eval.hash_gold_artifacts)
    gold_dir = tempfile.mkdtemp(prefix="osw_gold_")
    gold_sha256 = {}
    try:
        if url:
            try:
                reward = _evaluate_with_retry(url, task, _action_history(answer), gold_dir)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"   # desktop unreachable / getter failed -> fall back
            # scoring can fail after the gold was already fetched -- still worth recording
            gold_sha256 = osworld_eval.hash_gold_artifacts(gold_dir, task)
    finally:
        # Task 11c: keep the files the getters placed in gold_dir -- result AND gold/expected
        # side -- before it's removed below. Runs whatever happened above: a clean score, a
        # scoring exception (some getters may already have written their file), or no
        # controller at all (a no-op then -- nothing was ever downloaded, manifest is []).
        eval_artifacts = _collect_eval_artifacts(gold_dir, out, task)
        # hashed/copied, no longer needed -- an unattended multi-day campaign otherwise leaks one
        # of these per run (found live 2026-08-16: 5400 stray temp dirs/files, 3.6GB, after
        # ~1000 run attempts -- see _mcp_config's own tempfile, cleaned up by its caller)
        shutil.rmtree(gold_dir, ignore_errors=True)
    if reward is not None:   # OSWorld's official evaluators
        return {"verdict": osworld_eval.reward_to_verdict(reward), "reward": reward,
                "reason": f"official {task.get('evaluator', {}).get('func')} -> {reward:.2f}",
                "source": "official", "gold_sha256": gold_sha256,
                "eval_artifacts": eval_artifacts}
    # fallback: no verified way to score without the official evaluator (see evaluate.py) --
    # always EVAL_ERROR, kept only as a diagnostic of whatever state we did capture.
    rec = {**evaluate.osworld_check(task, answer, None, out), "source": "offline_fallback",
           "gold_sha256": gold_sha256, "eval_artifacts": eval_artifacts}
    if err:
        rec["reason"] = f"{rec['reason']} (official eval errored: {err})"
    return rec


def _environment_error_rec(task, setup_error):
    """A task whose environment was never correctly prepared is not a task the agent could have
    passed OR failed — it's a third outcome, excluded from pass-rate (see core.reporting)."""
    return {
        "result": {"id": task["id"], "bucket": tasks.bucket_of(task),
                  "instruction": task["instruction"], "answer": "",
                  "setup_error": setup_error},
        "eval": {"id": task["id"], "verdict": "ENVIRONMENT_ERROR",
                "reason": setup_error, "source": "environment"},
    }


def _rate_limit_result_rec(task, meta):
    """Telemetry for a run the CLI never actually attempted (see _rate_limit_infra_rec) --
    same shape as a normal result.json so it stays inspectable, minus provenance/eval_state
    (the caller fills provenance; there's no desktop state to capture)."""
    telemetry = _agent_telemetry(meta)
    telemetry["agent_clean_finish"] = False
    telemetry.update(_model_mismatch(meta))
    return {"id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": "", **telemetry}


def _save_conversation_transcript(meta, out, task_id=None):
    """Copy Claude Code's own session transcript -- the full turn-by-turn conversation,
    including every tool call and result, not just the final answer -- into this run's output
    dir as conversation.jsonl. Claude Code already writes one per `-p` invocation to
    ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl, keyed by the same session_id already
    captured in agent_telemetry; this is a copy of data that already exists, not a new capture
    mechanism (no change to the claude invocation itself). Globs by session_id rather than
    reconstructing the cwd-encoding scheme (undocumented, could change) for robustness.
    Added 2026-08-24, applies to runs from here forward only -- not backfilled.

    Still best-effort -- a missing/rotated transcript must never fail an otherwise good run --
    but no longer SILENTLY best-effort. It returns a status the caller folds into result.json
    and prints a warning on failure.

    Why this matters enough to instrument: the transcript is the ONLY evidence of what the
    agent actually did. Nothing else on disk records it -- `agent_permission_denials` is
    structurally always empty under --dangerously-skip-permissions, and agent_output.txt holds
    only the final answer (verified 2026-09-04: neither surfaces a single one of the 5 tasks
    that genuinely reached the host). A run whose transcript silently failed to copy is a run
    whose tool use is unverifiable forever, and the earlier version swallowed exactly that.
    Recording the status per run means coverage is one scan of result.json away instead of a
    forensic pass over driver logs that may have already rotated."""
    session_id = meta.get("session_id")
    where = f"{task_id or '?'} -> {out.name}"
    if not session_id:
        print(f"[osworld] WARNING transcript not saved ({where}): no session_id in the CLI "
              f"envelope, nothing to look up")
        return {"transcript_saved": False, "transcript_error": "no session_id"}
    try:
        matches = list(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
    except OSError as e:
        print(f"[osworld] WARNING transcript not saved ({where}): glob failed: {e}")
        return {"transcript_saved": False, "transcript_error": f"glob failed: {e}"}
    if not matches:
        print(f"[osworld] WARNING transcript not saved ({where}): no transcript on disk for "
              f"session {session_id} (rotated, or written under an unexpected path)")
        return {"transcript_saved": False, "transcript_error": f"no file for {session_id}"}
    try:
        dest = out / "conversation.jsonl"
        shutil.copyfile(matches[0], dest)
        return {"transcript_saved": True, "transcript_bytes": dest.stat().st_size}
    except OSError as e:
        print(f"[osworld] WARNING transcript not saved ({where}): copy failed: {e}")
        return {"transcript_saved": False, "transcript_error": f"copy failed: {e}"}


def _rate_limit_infra_rec(task, api_error_status):
    """Deliberately NOT eval.json (see write_infra_error): a subscription session limit is
    transient on a fixed reset clock, not evidence of agent success or failure. Written so
    is_done() still sees this run as undone -- a later `--runs` invocation retries it
    automatically once the limit clears, no --force needed."""
    return {"id": task["id"], "outcome": "RATE_LIMITED",
            "error_type": "APIError", "error": f"api_error_status={api_error_status}",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
