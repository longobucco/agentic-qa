"""OSWorld Verified replica driven by GPT Astra through the Codex CLI.

The environment, MCP action surface and official evaluator are identical to agent_computer;
only the model/agent runtime changes. Results live under ``agent_computer_astra``.
"""
import json
import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config, official_protocol, tasks
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners import astra_common
from benchmarks.osworld.runners.common import mcp_child_env, protocol_wait
from benchmarks.osworld.runners.agent_computer import (
    _a11y_health, _annotate_incidental, _bounded, _capture_eval_state, _environment_error_rec,
    _official_system_prompt, _provenance, _score,
)
from core import results as results_io
from core.agent_loop import extract_answer, preview
from core.codex_loop import (
    APPROVAL_MODE, DISABLED_FEATURES, _is_tool_item, _parse_events, allowed_mcp_tools,
    base_instructions_file, build_codex_cmd, offered_tools_from_trace, run_codex_meta,
    session_context,
)


def _astra_prompt(task):
    """Codex never has run_python (core/codex_loop.py forces OSW_RESTRICT_RUN_PYTHON=1 in the MCP
    child), so the prompt must never advertise it, whatever the runner process's env says."""
    return agent_prompt(task, offer_run_python=False)


_LOCK = config.ASTRA_CAMPAIGN_LOCK

# Official protocol (config.OFFICIAL): the upstream system prompt replaces Codex's own base
# instructions (core.codex_loop.BASE_INSTRUCTIONS_KEY) and the bare instruction is the user turn.
# Everything else Codex would put in front of the model is removed with its supported config keys
# below -- each verified on 0.153.4 against the session's rollout and the tool list the server
# echoes (task-7-report.md). --ignore-user-config/--ignore-rules, DISABLED_FEATURES and the fresh
# empty cwd (run(), below) already apply to every run.
CODEX_ISOLATION_CONFIG = (
    "include_environment_context=false",              # <environment_context>: cwd, shell, tz, date
    "include_permissions_instructions=false",         # <permissions instructions>
    "include_collaboration_mode_instructions=false",  # <collaboration_mode>
    "include_apps_instructions=false",                # apps/connectors instructions
    "skills.include_instructions=false",              # the host's ~/.codex/skills listing
    "project_doc_max_bytes=0",                        # AGENTS.md project docs from the cwd
    'web_search="disabled"',                          # the hosted web search (exec.web__run)
    "tools.experimental_request_user_input.enabled=false",   # functions.request_user_input
    "agents.max_threads=1",                           # no sub-agent can run beside the root one
)
# What the model is still offered under CODEX_ISOLATION_CONFIG on 0.153.4, as named by
# core.codex_loop.offered_tools_from_trace: no supported key removes these (the collaboration
# namespace survives `--disable multi_agent`/`multi_agent_v2`; exec is the Code Mode host MCP calls
# go through, and its nested helpers come with it). `computer` itself is deferred by Codex (reached
# through the host's ALL_TOOLS) so it is not listed. None of them can reach the guest; a call to
# any of them is a ToolSurfaceViolation and the run is never scored (run(), below).
CODEX_TOLERATED_TOOLS = frozenset({
    "functions.exec", "functions.wait", "functions.request_user_input_async",
    "exec.apply_patch", "exec.clock__curr_time", "exec.list_mcp_resources",
    "exec.list_mcp_resource_templates", "exec.read_mcp_resource",
    "collaboration.followup_task", "collaboration.interrupt_agent", "collaboration.list_agents",
    "collaboration.send_message", "collaboration.spawn_agent", "collaboration.wait_agent",
})
OFFICIAL_CODEX_TOOL = "computer"
# Makes Codex log the server's `response.created` events (which echo the offered tools) to stderr.
# Used by the session preflight only: a run's stderr is scanned for rate-limit text.
_TOOL_TRACE_LOG = "error,tungstenite::protocol=trace"


def codex_events_actions(events):
    """(agent_message texts, `computer` call arguments) from a Codex `exec --json` event log, in
    order -- what official_protocol.final_action needs. A call is counted once (its started and
    completed events share an id; a call cut off before completing still counts); arguments
    given as a JSON string are parsed."""
    texts, calls = [], {}
    for n, event in enumerate(events or []):
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message" and event.get("type") == "item.completed":
            texts.append(item.get("text") or "")
        elif item.get("type") == "mcp_tool_call" and item.get("tool") == OFFICIAL_CODEX_TOOL:
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls[item.get("id") or f"#{n}"] = args if isinstance(args, dict) else {}
    return texts, list(calls.values())


def _non_computer_tool_calls(events):
    """Name of every tool call in the event log other than `computer` (deduplicated by id)."""
    names = {}
    for n, event in enumerate(events or []):
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or not _is_tool_item(item):
            continue
        if item.get("type") == "mcp_tool_call" and item.get("tool") == OFFICIAL_CODEX_TOOL:
            continue
        names[item.get("id") or f"#{n}"] = (item.get("tool") or item.get("name")
                                            or item.get("type"))
    return list(names.values())


def _rollout_path(session_id):
    """Codex's own rollout for a session (~/.codex/sessions), or None."""
    path = session_context(session_id).get("rollout_path") if session_id else None
    return Path(path) if path else None


def _remove_instructions_file(cmd):
    path = base_instructions_file(cmd)
    if path:
        Path(path).unlink(missing_ok=True)


def _official_cmd(prompt, workdir, controller_url):
    """The official-protocol `codex exec`: upstream's system prompt as the base instructions, the
    prompt as the only user turn, host context removed (CODEX_ISOLATION_CONFIG)."""
    return build_codex_cmd(
        prompt, model=config.ASTRA_MODEL, cwd=workdir, controller_url=controller_url,
        reasoning_effort=config.ASTRA_REASONING_EFFORT or None, mcp_extra_env=mcp_child_env(),
        base_instructions=_official_system_prompt(), extra_config=CODEX_ISOLATION_CONFIG,
    )


def official_session_preflight():
    """Probe the installed CLI once, isolated exactly like an official run (same command, fresh
    empty cwd, prompt "reply ok"), and refuse the campaign when the model is offered a tool
    outside CODEX_TOLERATED_TOOLS (or the offered tools can't be read), or when its rollout is
    missing or shows host context reaching the model (astra_common.codex_rollout_context_leaks)."""
    workdir = Path(tempfile.mkdtemp(prefix="osw-astra-"))
    cmd = _official_cmd("reply ok", workdir, "")
    try:
        meta = run_codex_meta(cmd, timeout=300,
                              env={**os.environ, "RUST_LOG": _TOOL_TRACE_LOG})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_instructions_file(cmd)
    offered = offered_tools_from_trace(meta.get("stderr"))
    if offered is None:
        raise SystemExit("official protocol: the isolation probe logged no offered-tool list "
                         f"(errors: {meta.get('errors')}); cannot verify the Codex tool surface")
    unexpected = [t for t in offered if t not in CODEX_TOLERATED_TOOLS]
    if unexpected:
        raise SystemExit(f"official protocol: the installed codex CLI offers tools outside "
                         f"gpt_astra.CODEX_TOLERATED_TOOLS: {', '.join(unexpected)}")
    rollout = _rollout_path(meta.get("session_id"))
    if rollout is None:
        raise SystemExit("official protocol: no Codex rollout for the isolation probe (session "
                         f"{meta.get('session_id')!r}); cannot verify no host context reaches "
                         "the agent")
    leaks = astra_common.codex_rollout_context_leaks(rollout)
    if leaks:
        raise SystemExit(f"official protocol: host context still reaches the agent despite "
                         f"CODEX_ISOLATION_CONFIG: {', '.join(leaks)} (probe rollout {rollout})")
# Codex/Astra-specific telemetry, rate-limit detection, tool audit and provenance now live in
# astra_common.py (shared with runners/gpt_astra_openbook.py) -- re-exported under their original
# names so this module's own callers/tests (patch.object(gpt_astra, "_codex_version", ...), etc.)
# keep resolving unchanged.
_api_error_status = astra_common.api_error_status
_estimated_api_cost = astra_common.estimated_api_cost
_codex_version = astra_common.codex_version
_rate_limit_rec = astra_common.rate_limit_rec
_write_tool_audit = astra_common.write_tool_audit


def _telemetry(meta, stderr=""):
    return astra_common.telemetry(meta, stderr)


def _provenance_astra(task, ctrl, started_at, codex_version, session_id=None):
    return astra_common.provenance_astra(
        task, ctrl, started_at, codex_version, model=config.ASTRA_MODEL,
        reasoning_effort=config.ASTRA_REASONING_EFFORT, session_id=session_id)


def _validate_campaign_lock():
    lock = json.loads(_LOCK.read_text())
    if lock["release"] != "osworld_verified" or config.RELEASE != "verified":
        raise SystemExit(f"Astra release differs from frozen OSWorld Verified: {config.RELEASE!r}")
    expected = {
        "model": config.ASTRA_MODEL,
        "reasoning_effort": config.ASTRA_REASONING_EFFORT,
        "codex_cli_version": config.ASTRA_CODEX_VERSION,
    }
    for key, actual in expected.items():
        if lock[key] != actual:
            raise SystemExit(f"Astra campaign lock mismatch for {key}: {actual!r} != {lock[key]!r}")
    population = lock["population"]
    ids_paths = population.get("paths") or [population["path"]]
    root = Path(__file__).resolve().parents[3]
    ids = [line for path in ids_paths for line in (root / path).read_text().splitlines()
           if line.strip() and not line.lstrip().startswith("#")]
    exclude_path = population.get("exclude_path")
    if exclude_path:
        excluded = {line.strip() for line in (root / exclude_path).read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
        ids = [task_id for task_id in ids if task_id not in excluded]
    ids_bytes = ("\n".join(ids) + "\n").encode()
    digest = hashlib.sha256(ids_bytes).hexdigest()
    if digest != population["sha256"]:
        raise SystemExit(f"Astra population hash mismatch: {digest}")
    if len(ids) != population["count"] or len(set(ids)) != len(ids):
        raise SystemExit("Astra population count/uniqueness differs from the frozen campaign lock")
    policy = lock["tool_policy"]
    if (policy["approval_mode"] != APPROVAL_MODE
            or tuple(policy["disabled_features"]) != DISABLED_FEATURES
            or tuple(policy["allowed_mcp_tools"]) != allowed_mcp_tools(
                config.ZOOM_BATCH, official=config.OFFICIAL)
            or (config.OFFICIAL
                and tuple(policy.get("isolation_config") or ()) != CODEX_ISOLATION_CONFIG)):
        raise SystemExit("Astra tool policy differs from the frozen campaign lock")


def preflight():
    osworld_eval.pinned_code_preflight()
    _validate_campaign_lock()
    # core.run writes harness.json after preflight; materialize defaults so a direct invocation
    # records the same campaign identity as the shell driver.
    os.environ.setdefault("OSW_ASTRA_MODEL", config.ASTRA_MODEL)
    os.environ.setdefault("OSW_ASTRA_REASONING_EFFORT", config.ASTRA_REASONING_EFFORT)
    os.environ.setdefault("OSW_ASTRA_CODEX_VERSION", config.ASTRA_CODEX_VERSION)
    astra_common.check_codex_cli(
        model=config.ASTRA_MODEL, reasoning_effort=config.ASTRA_REASONING_EFFORT,
        codex_cli_version=config.ASTRA_CODEX_VERSION,
    )
    if config.OFFICIAL:
        official_session_preflight()


def _official_answer(transcript, text):
    """Upstream's termination rule over the whole session (official_protocol.final_action), read
    from the event log this run just saved; without one, only the final text is left to judge."""
    if transcript.stat().st_size > 0:
        return official_protocol.final_action(
            *codex_events_actions(_parse_events(transcript.read_text())[0]))
    return official_protocol.final_action([text], [])


def _official_audit(events, transcript_saved, session_id):
    """Per-run audit, same fields as the Claude arm: tool calls other than `computer` (from the
    saved event log plus Codex's rollout, which alone shows what Code Mode scripts called), host
    context kinds that reached the agent (from the rollout), and the offered tools. None when a
    source is missing, so an unverifiable run isn't recorded as clean. Offered tools are always
    None here: Codex records them in neither the event log nor the rollout
    (official_session_preflight verifies them once per campaign instead)."""
    rollout = _rollout_path(session_id)
    calls = leaks = None
    try:
        if rollout:
            leaks = astra_common.codex_rollout_context_leaks(rollout)
            if transcript_saved:
                calls = (_non_computer_tool_calls(events)
                         + astra_common.codex_rollout_tool_calls(rollout))
    except OSError:
        calls = leaks = None
    return {"agent_non_computer_tool_calls": calls,
            "agent_context_leaks": leaks,
            "agent_offered_tools": None}


def run(task, *, env, out, refs=None, dry=False):
    if not dry:
        # See gpt_astra_openbook.run's identical guard: without this, a stale eval.json from an
        # earlier attempt at this run-dir survives a retry that short-circuits before scoring
        # (e.g. into an infra error), and report.py reads it as the current verdict.
        (Path(out) / "eval.json").unlink(missing_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    controller_url = ctrl.base_url if ctrl else config.CONTROLLER_URL
    setup_error = getattr(env, "setup_error", None)
    if setup_error and not dry:
        rec = _environment_error_rec(task, setup_error)
        rec["result"]["provenance"] = _provenance_astra(
            task, ctrl, started_at, _codex_version())   # no session: the agent never ran
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""

    # A fresh, empty cwd per run (removed afterwards): no AGENTS.md, no repo path.
    workdir = Path(tempfile.mkdtemp(prefix="osw-astra-"))
    if config.OFFICIAL:
        cmd = _official_cmd(task["instruction"], workdir, controller_url)
    else:
        cmd = build_codex_cmd(
            _astra_prompt(task), model=config.ASTRA_MODEL, cwd=workdir,
            controller_url=controller_url, reasoning_effort=config.ASTRA_REASONING_EFFORT or None,
            mcp_extra_env=mcp_child_env(),
        )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_instructions_file(cmd)
        return None

    protocol_wait(config.POST_SETUP_WAIT_S)   # upstream: sleep 60 after reset, before step 1
    try:
        meta = run_codex_meta(cmd, timeout=config.TASK_TIMEOUT)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_instructions_file(cmd)

    # Preserve the complete provider trajectory; unlike Claude Code this is emitted directly by
    # the headless invocation, so no lookup in a user-global session directory is necessary.
    transcript = out / "conversation.jsonl"
    transcript.write_text(meta.pop("raw", ""))
    stderr = meta.pop("stderr", "")
    if stderr:
        (out / "codex_stderr.txt").write_text(stderr)
    events = meta.pop("events", None)
    audit = _write_tool_audit(out, events)
    text = meta.get("result", "")
    answer = extract_answer(text)
    clean_finish = bool(answer) and not meta.get("is_error", False)
    official_telemetry = {}
    if config.OFFICIAL:
        # Upstream has no ANSWER line: DONE unless [INFEASIBLE]/a fail action; the episode ends
        # cleanly when the CLI does (no error, no timeout).
        answer = _official_answer(transcript, text)
        clean_finish = not meta.get("is_error", False)
        official_telemetry = _official_audit(events, transcript.stat().st_size > 0,
                                             meta.get("session_id"))
    results_io.write_output(out, text)
    telemetry = _telemetry(meta, stderr)
    telemetry["agent_clean_finish"] = clean_finish
    provenance = _provenance_astra(task, ctrl, started_at, _codex_version(),
                                   session_id=meta.get("session_id"))
    # Measured, not asserted: a Codex run that dies before emitting JSONL still creates the
    # file, and "transcript_saved": True on an empty one is the same species of lie as a
    # provenance field that reports an intention (see the Sonnet evaluator-pin post-mortem).
    transcript_bytes = transcript.stat().st_size
    trace = {"transcript_saved": transcript_bytes > 0, "transcript_bytes": transcript_bytes,
             "contaminated": audit["contaminated"],
             "contamination_evidence": audit["evidence"], **official_telemetry}

    api_error = telemetry["agent_api_error_status"]
    if api_error:
        # Any throttled run, whether or not the agent got some actions in first. Sonnet's
        # campaign scored one such run as a plain FAILURE (881deb30/run_3: 9 turns, then a 429)
        # and it took a manual audit to find; a verdict on a truncated agent measures the API's
        # patience, not the agent. infra_error.json keeps it out of every rate and lets resume
        # retry it.
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, **trace, **telemetry,
        })
        results_io.write_infra_error(out, _rate_limit_rec(task, api_error))
        return ""

    if config.OFFICIAL and official_telemetry["agent_non_computer_tool_calls"] is None:
        # The agent ran but what it called can't be verified (no event log or no rollout: Code
        # Mode calls are only in the rollout). Not scored, same as a violation -- an unaudited
        # run could have used a tool other than `computer`.
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, **trace, **telemetry,
        })
        results_io.write_infra_error(out, {
            "id": task["id"], "outcome": "HARNESS_ERROR",
            "error_type": "ToolAuditUnavailable",
            "error": (f"tool use unverifiable: transcript_saved={trace['transcript_saved']}, "
                      f"rollout={_rollout_path(meta.get('session_id'))}"),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return ""

    if meta.get("non_mcp_tool_calls") or official_telemetry.get("agent_non_computer_tool_calls"):
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, **trace, **telemetry,
        })
        names = (official_telemetry.get("agent_non_computer_tool_calls")
                 or meta.get("tool_names") or [])
        if config.OFFICIAL:
            # Ruling (task 7b): under the official protocol a tool-surface violation is a
            # TERMINAL failure, not an infra error -- excluding or retrying it would
            # selectively resample toward runs that happen not to violate, biasing the score
            # upward either way. Score it 0 and label it clearly instead; never infra_error.json
            # (that would leave is_done() False and a later --runs invocation would re-roll it).
            results_io.write_eval(out, {
                "id": task["id"], "verdict": "FAILURE", "reward": 0.0, "source": "harness",
                "reason": f"tool surface violation: {', '.join(names)}",
                "tool_surface_violation": names,
            })
        else:
            # Legacy (flag-off) path: unchanged -- an infra error, retried on resume.
            results_io.write_infra_error(out, {
                "id": task["id"], "outcome": "HARNESS_ERROR",
                "error_type": "ToolSurfaceViolation",
                "error": (f"Codex used tools other than `computer`: "
                          f"{official_telemetry['agent_non_computer_tool_calls']}"
                          if official_telemetry.get("agent_non_computer_tool_calls")
                          else f"Codex used non-MCP tools: {meta.get('tool_names')}"),
                "at": datetime.now(timezone.utc).isoformat(),
            })
        return ""

    protocol_wait(config.PRE_EVAL_WAIT_S)   # upstream: sleep 20 before evaluate
    # Some official evaluators materialize their result in `postconfig` (for example a
    # vm_file such as /home/user/log.txt). Capture only after scoring, otherwise the read sees
    # a legitimate 404 before the evaluator has created the artifact and leaves a diagnosability
    # gap on an otherwise valid verdict.
    rec = _annotate_incidental(
        _bounded("scoring", _score, ctrl, task, answer, out), clean_finish)
    if answer.strip().upper() == "FAIL":
        # Official OSWorld scoring short-circuits on an agent-declared FAIL, before its
        # postconfig can create a vm_file result. A download here is guaranteed to 404 and
        # obscures a legitimate agent verdict with a harness-looking warning.
        eval_state = {
            "capture_status": "not_applicable",
            "reason": "agent returned FAIL; official evaluator skipped postconfig",
        }
    else:
        eval_state = _bounded(
            "eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    results_io.write_result(out, {
        "id": task["id"], "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"], "answer": answer,
        "eval_state": eval_state, "provenance": provenance,
        **trace, **telemetry,
        # Mirrors agent_computer.run's use of the same probe (runners/common.py's
        # docstring): the Astra runner never recorded this, so every closed-book run with a
        # genuinely blank desktop (screenshot/`a11y_tree` never showing real content, despite
        # `agent_stop_reason=completed`) looked like a plain agent FAILURE with no on-disk
        # signal to tell the two apart -- confirmed live across 16 of 32 closed-book "0/3" tasks.
        **_a11y_health(ctrl),
    })
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
