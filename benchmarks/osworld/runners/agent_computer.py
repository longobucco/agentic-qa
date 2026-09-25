"""OSWorld runner: claude -p + the OSWorld MCP drive the desktop, then score with OSWorld's
official evaluators while the desktop is still live (self_eval) and write eval.json. Falls back
to the offline checker in evaluate.py when desktop_env isn't importable.

Shared, policy-free primitives (MCP config, telemetry, provenance, the post-run watchdog,
transcript capture, scoring) live in runners/common.py. Re-imported here by name so every
existing external reference to e.g. `agent_computer._mcp_config` keeps resolving -- see
benchmarks/osworld/tests/test_runner.py's characterization tests for the frozen contract.
"""
import contextlib
import json
import os
import shutil
import tempfile

from datetime import datetime, timezone

from benchmarks.osworld import config, official_protocol, tasks
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.runners.common import (
    _a11y_health, _action_history, _agent_telemetry, _annotate_incidental, _bounded,
    _capture_eval_state, _environment_error_rec, _evaluate_with_retry, _evaluator_provenance,
    _mcp_config, _model_mismatch, _POST_RUN_TIMEOUT_S, _provenance, _rate_limit_infra_rec,
    _rate_limit_result_rec, _save_conversation_transcript, _score, _served_by, claude_cli_version,
    claude_env, claude_session_file, claude_transcript_actions, claude_transcript_context_leaks,
    claude_transcript_offered_tools, claude_transcript_tool_names, claude_workdir_session_file,
    mcp_unavailable_infra_rec, official_max_turns, official_probe_already_passed, protocol_wait,
    read_mcp_state, reset_mcp_state,
)
from core.agent_loop import _run_raw, build_claude_cmd, preview, run_claude_meta
from core import results as results_io


# Every non-MCP tool Claude Code offers, all denied so the agent acts only through the
# `computer` tool, as upstream's agent does. Read from the `init` event's `tools` of
# `claude -p --output-format stream-json --verbose` on CLI 2.1.280, 2026-09-24 -- a newer CLI
# may add tools, so re-read it when the pinned CLI changes.
CLAUDE_BUILTIN_TOOLS = [
    "Task", "Artifact", "ArtifactComments", "ArtifactData", "Bash", "CronCreate", "CronDelete",
    "CronList", "DesignSync", "Edit", "EnterWorktree", "ExitWorktree", "ListAgents", "Monitor",
    "NotebookEdit", "PushNotification", "Read", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "TaskStop", "ToolSearch", "WebFetch", "WebSearch",
    "Write",
    # Not in the init event's `tools`, yet offered to the model: seen in the session's
    # system-prompt snapshot (CLI 2.1.280, 2026-09-24, task-6-report.md fix round 1).
    "Glob", "Grep", "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
]
OFFICIAL_TOOL = "mcp__osworld__computer"
# Official-run isolation from host context (hooks, plugins, user/project settings), verified
# live on CLI 2.1.280 (task-6-report.md, fix round 1): no setting sources means no user
# settings, so no plugins or their SessionStart hooks; disableAllHooks drops any hook left.
# Together with the empty cwd (_isolated_workdir) this also removes project memory/CLAUDE.md,
# git status and the repo path. `--bare` would go further but requires API-key auth.
CLAUDE_ISOLATION_FLAGS = ["--setting-sources", "", "--settings",
                          json.dumps({"disableAllHooks": True})]
# The one kind no supported CLI mechanism removes under OAuth (subscription) auth: the account
# e-mail, injected from the OAuth account as session_context.userEmail. Recorded per run in
# agent_context_leaks; the preflight tolerates only this.
OFFICIAL_TOLERATED_LEAKS = ("user_email",)


@contextlib.contextmanager
def _isolated_workdir():
    """A fresh, empty cwd for the official CLI session (removed afterwards): no repo CLAUDE.md,
    no project auto-memory keyed by the repo path, no git status, no repo path in the
    environment block. Verified live on CLI 2.1.280 (task-6-report.md, fix round 1)."""
    d = tempfile.mkdtemp(prefix="osw_claude_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def official_tool_preflight():
    """Probe the installed CLI once, isolated exactly like an official run (same deny list,
    isolation flags, system prompt and empty cwd; no MCP server), and refuse the campaign when:
    - its `init` event offers a non-MCP tool CLAUDE_BUILTIN_TOOLS doesn't name (it would stay
      available to the agent), or any MCP tool other than OFFICIAL_TOOL (a user/plugin MCP
      server leaking in). The init event is searched for, not assumed first -- hooks can emit
      events before it;
    - its session transcript is missing, shows host context reaching the model beyond
      OFFICIAL_TOLERATED_LEAKS (see common.claude_transcript_context_leaks), or shows the
      model offered any tool other than OFFICIAL_TOOL (the init event does not list every
      such tool).
    Runs with the same child env as a real run (claude_env(): auto-updater off)."""
    kw = _official_cmd_kwargs({"instruction": "reply ok"})
    cmd = (["claude", "-p", kw["prompt"], "--output-format", "stream-json", "--verbose",
            "--max-turns", "1", *kw["extra"], "--system-prompt", kw["system_prompt"]]
           + (["--model", config.MODEL] if config.MODEL else []))
    with _isolated_workdir() as workdir:
        stream = _run_raw(cmd, timeout=300, cwd=workdir, env=claude_env())
    init = None
    for line in (stream or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            init = ev
            break
    if init is None or not isinstance(init.get("tools"), list):
        raise SystemExit("official protocol: no init event with a tool list from `claude -p "
                         "--output-format stream-json`; cannot verify the built-in deny list")
    unknown = [t for t in init["tools"]
               if not t.startswith("mcp__") and t not in CLAUDE_BUILTIN_TOOLS]
    if unknown:
        raise SystemExit(f"official protocol: the installed claude CLI offers built-in tools "
                         f"not in CLAUDE_BUILTIN_TOOLS (they would not be denied): "
                         f"{', '.join(unknown)} -- add them to agent_computer.CLAUDE_BUILTIN_TOOLS")
    foreign_mcp = [t for t in init["tools"] if t.startswith("mcp__") and t != OFFICIAL_TOOL]
    if foreign_mcp:
        raise SystemExit(f"official protocol: the installed claude CLI offers MCP tools other "
                         f"than {OFFICIAL_TOOL} despite --strict-mcp-config: "
                         f"{', '.join(foreign_mcp)}")
    session = claude_session_file(init.get("session_id"))
    if session is None:
        raise SystemExit("official protocol: no session transcript for the isolation probe "
                         f"(session {init.get('session_id')!r}); cannot verify no host context "
                         "reaches the agent")
    offered = claude_transcript_offered_tools(session)
    if offered is None:
        raise SystemExit(f"official protocol: the isolation probe recorded no offered-tool "
                         f"snapshot; cannot verify the deny list (probe transcript {session})")
    still = [t for t in offered if t != OFFICIAL_TOOL]
    if still:
        raise SystemExit(f"official protocol: tools other than {OFFICIAL_TOOL} still offered to "
                         f"the model despite --disallowedTools/--strict-mcp-config: "
                         f"{', '.join(still)} -- a built-in belongs in "
                         f"agent_computer.CLAUDE_BUILTIN_TOOLS (probe transcript {session})")
    leaks = [k for k in claude_transcript_context_leaks(session)
             if k not in OFFICIAL_TOLERATED_LEAKS]
    if leaks:
        raise SystemExit(f"official protocol: host context still reaches the agent despite the "
                         f"isolation flags: {', '.join(leaks)} (probe transcript {session})")
    return None


def claude_version_preflight():
    """The official protocol was verified on one Claude Code CLI (config.CLAUDE_CODE_VERSION):
    refuse any other, or an unreadable `claude --version` (run with the real runs' env)."""
    found = claude_cli_version(cached=False)
    if found != config.CLAUDE_CODE_VERSION:
        raise SystemExit(f"official protocol: claude CLI version mismatch: campaign pins "
                         f"{config.CLAUDE_CODE_VERSION} (OSW_CLAUDE_CODE_VERSION), found {found}")


def preflight():
    """Sonnet runner preflight: refuse an unpinned model, then today's pinned-code check, the
    pinned CLI version and the built-in tool drift guard. The last one is a live model call: a
    campaign driver child skips it when the driver already ran it for this driver run
    (common.official_probe_already_passed) -- every cheap check still runs.

    Re-checks legacy knobs directly: config refuses them at import, but core.run loads .env
    (which can carry a stale legacy knob) after benchmark.build() has already imported config,
    so the import-time check alone would miss one."""
    config._refuse_legacy_knobs()
    if not config.MODEL:
        raise SystemExit("OSW_MODEL is required: the official Claude runner never runs the "
                         "CLI's default model")
    osworld_eval.pinned_code_preflight()
    claude_version_preflight()
    if not official_probe_already_passed():
        official_tool_preflight()


def _effort_kwargs():
    """Protocol knobs (OSW_EFFORT / OSW_MAX_OUTPUT_TOKENS) as call kwargs, present only when set:
    with neither set, the baseline build_claude_cmd/run_claude_meta calls stay exactly as they
    were."""
    return {"effort": config.EFFORT} if config.EFFORT else {}


def _env_kwargs():
    return {"env": claude_env()}


def _run_provenance(task, ctrl, started_at):
    """_provenance, plus the runtime fields the Codex arm also records
    (astra_common.provenance_astra)."""
    prov = _provenance(task, ctrl, started_at)
    prov["agent_runtime"] = "claude_code"
    prov["agent_runtime_version"] = claude_cli_version()
    return prov


def _official_clean_finish(meta):
    """Upstream has no ANSWER line: an episode ends when the model stops calling tools (or the
    budget runs out). Clean = the CLI reported a normal end -- not error_max_turns, not an
    error, and not a timeout (which leaves no parseable envelope, so meta is {})."""
    return meta.get("subtype") == "success" and not meta.get("is_error", False)


def _official_audit(out, transcript):
    """Per-run audit from this run's transcript: tool calls other than `computer`, host
    context kinds that reached the agent, the tools the model was offered and those among them
    other than `computer`. All None if it wasn't saved, so an unverifiable run isn't recorded
    as clean."""
    path = out / "conversation.jsonl"
    unknown = {"agent_non_computer_tool_calls": None, "agent_context_leaks": None,
               "agent_offered_tools": None, "agent_unexpected_offered_tools": None}
    if not transcript.get("transcript_saved"):
        return unknown
    try:
        offered = claude_transcript_offered_tools(path)
        return {"agent_non_computer_tool_calls":
                    [n for n in claude_transcript_tool_names(path) if n != OFFICIAL_TOOL],
                "agent_context_leaks": claude_transcript_context_leaks(path),
                "agent_offered_tools": offered,
                "agent_unexpected_offered_tools":
                    None if offered is None else [t for t in offered if t != OFFICIAL_TOOL]}
    except OSError:
        return unknown


def _recover_transcript(workdir, out, transcript):
    """When the CLI envelope carried no session_id (e.g. killed at TASK_TIMEOUT: no envelope at
    all), find the session by the run's own fresh cwd instead (common.claude_workdir_session_file)
    and save it as conversation.jsonl, so the answer and the audits still come from it."""
    session = claude_workdir_session_file(workdir)
    if session is None:
        return transcript
    dest = out / "conversation.jsonl"
    try:
        shutil.copyfile(session, dest)
    except OSError as e:
        print(f"[osworld] WARNING transcript recovery from {session} failed: {e}")
        return transcript
    return {"transcript_saved": True, "transcript_bytes": dest.stat().st_size,
            "transcript_recovered_from_workdir": True,
            "transcript_recovered_session_id": session.stem}


def _official_system_prompt():
    return official_protocol.system_prompt(
        max_steps=config.MAX_STEPS,
        client_password=config.KVM_CLIENT_PASSWORD if config.BACKEND == "kvm" else "")


def _official_cmd_kwargs(task):
    """build_claude_cmd kwargs under the official protocol: the bare instruction as the user
    turn, upstream's system prompt, only the `computer` tool, and the deny list covering every
    other built-in -- so --disallowedTools/--strict-mcp-config appear exactly once."""
    return {
        "prompt": task["instruction"],
        "system_prompt": _official_system_prompt(),
        "allowed_tools": [OFFICIAL_TOOL],
        "max_turns": official_max_turns(),
        "extra": ["--disallowedTools", *CLAUDE_BUILTIN_TOOLS, "--strict-mcp-config",
                  *CLAUDE_ISOLATION_FLAGS],
    }


def _official_answer(out, text, transcript):
    """Upstream's termination rule over the whole session (official_protocol.final_action),
    read from the transcript this run just saved; without one (a leftover file from an earlier
    attempt doesn't count), only the final text is left to judge."""
    if transcript.get("transcript_saved"):
        try:
            return official_protocol.final_action(
                *claude_transcript_actions(out / "conversation.jsonl"))
        except OSError:
            pass
    return official_protocol.final_action([text], [])


def run(task, *, env, out, refs=None, dry=False):
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    controller_url = ctrl.base_url if ctrl else config.CONTROLLER_URL

    setup_error = getattr(env, "setup_error", None)
    if setup_error and not dry:
        rec = _environment_error_rec(task, setup_error)
        rec["result"]["provenance"] = _run_provenance(task, ctrl, started_at)
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""

    # The MCP server also writes its liveness/step state into this run's dir.
    mcp_config_path = _mcp_config(controller_url, out_dir=out)
    official = _official_cmd_kwargs(task)
    cmd = build_claude_cmd(
        official.pop("prompt"),
        model=config.MODEL or None,
        mcp_config=mcp_config_path,
        **official,
        **_effort_kwargs(),
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        os.unlink(mcp_config_path)
        return None

    reset_mcp_state(out)   # only this run's server may prove it started
    # As gpt_astra.run does: a stale eval.json from an earlier attempt at this run dir must
    # not survive a retry that now ends in an infra error (unaudited / no MCP server).
    (out / "eval.json").unlink(missing_ok=True)
    protocol_wait(config.POST_SETUP_WAIT_S)   # upstream: sleep 60 after reset, before step 1

    workdir = None
    try:
        # An empty temp cwd isolates the session from repo/project context.
        with _isolated_workdir() as workdir:
            meta = run_claude_meta(cmd, timeout=config.TASK_TIMEOUT, cwd=workdir, **_env_kwargs())
    finally:
        # written fresh per run (NamedTemporaryFile(delete=False)); the claude subprocess has
        # exited by now (run_claude_meta blocks until it does) so it's safe to remove. Without
        # this an unattended multi-day campaign leaks one of these per run indefinitely (found
        # live 2026-08-16 -- see the matching cleanup on gold_dir in common._score).
        try:
            os.unlink(mcp_config_path)
        except OSError:
            pass
    api_error_status = meta.get("api_error_status")
    if api_error_status:
        # The CLI itself hit an API-level error (observed live: 429 subscription session
        # limit, "You've hit your session limit") before the agent acted at all -- num_turns=1,
        # cost=$0. Scoring the untouched desktop now would silently mint a SUCCESS/FAILURE that
        # measures our subscription throttling, not the model (found live: 14/27 runs in one
        # batch, all within the same ~8-minute window).
        results_io.write_output(out, meta.get("result", ""))
        result_rec = _rate_limit_result_rec(task, meta)
        result_rec["provenance"] = _run_provenance(task, ctrl, started_at)
        # capture BEFORE write_result so the transcript status rides in the record rather
        # than being lost -- a rate-limited run legitimately has no tool calls, so its
        # transcript is a stub, and only this flag distinguishes "stub because throttled"
        # from "transcript failed to copy".
        result_rec.update(_save_conversation_transcript(meta, out, task["id"]))
        results_io.write_result(out, result_rec)
        results_io.write_infra_error(out, _rate_limit_infra_rec(task, api_error_status))
        return ""

    text = meta.get("result", "")
    results_io.write_output(out, text)
    transcript = _save_conversation_transcript(meta, out, task["id"])
    if not transcript.get("transcript_saved") and not meta.get("session_id"):
        transcript = _recover_transcript(workdir, out, transcript)
    answer = _official_answer(out, text, transcript)
    clean_finish = _official_clean_finish(meta)
    official_telemetry = _official_audit(out, transcript)
    mcp_state = read_mcp_state(out)
    official_telemetry["agent_steps_used"] = mcp_state.get("steps_used") if mcp_state else None

    telemetry = _agent_telemetry(meta)
    telemetry["agent_clean_finish"] = clean_finish

    if (official_telemetry["agent_non_computer_tool_calls"] is None or mcp_state is None):
        # Unscored, retried (infra_error.json, never eval.json), same rule as the Codex arm:
        # either what the agent called can't be verified (no transcript, even recovered), or
        # the MCP server never started, so the agent had no `computer` tool at all.
        results_io.write_result(out, {
            "id": task["id"],
            "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"],
            "answer": answer,
            "provenance": _run_provenance(task, ctrl, started_at),
            **transcript,
            **_model_mismatch(meta),
            **telemetry,
            **official_telemetry,
        })
        if official_telemetry["agent_non_computer_tool_calls"] is None:
            infra = {"id": task["id"], "outcome": "HARNESS_ERROR",
                     "error_type": "ToolAuditUnavailable",
                     "error": (f"tool use unverifiable: no transcript "
                               f"({transcript.get('transcript_error')})"),
                     "at": datetime.now(timezone.utc).isoformat()}
        else:
            infra = mcp_unavailable_infra_rec(task)
        results_io.write_infra_error(out, infra)
        return ""

    non_computer = official_telemetry.get("agent_non_computer_tool_calls")
    if non_computer:
        # Ruling (task 7b), parity with the Codex arm (runners/gpt_astra.py run()): a
        # tool-surface violation is a TERMINAL failure, not something to skip or retry --
        # either would selectively resample toward runs that happen not to violate, biasing
        # the score. `None` (audit unverifiable, e.g. no transcript) is left untouched here,
        # same as before this task.
        results_io.write_result(out, {
            "id": task["id"],
            "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"],
            "answer": answer,
            "provenance": _run_provenance(task, ctrl, started_at),
            **transcript,
            **_model_mismatch(meta),
            **telemetry,
            **official_telemetry,
            **_a11y_health(ctrl),
        })
        results_io.write_eval(out, {
            "id": task["id"], "verdict": "FAILURE", "reward": 0.0, "source": "harness",
            "reason": f"tool surface violation: {', '.join(non_computer)}",
            "tool_surface_violation": non_computer,
        })
        return answer

    protocol_wait(config.PRE_EVAL_WAIT_S)   # upstream: sleep 20 before evaluate
    eval_state = _bounded("eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"],
        "answer": answer,
        "eval_state": eval_state,
        "provenance": _run_provenance(task, ctrl, started_at),
        **transcript,
        **_model_mismatch(meta),
        **telemetry,
        **official_telemetry,
        **_a11y_health(ctrl),
    })
    rec = _annotate_incidental(_bounded("scoring", _score, ctrl, task, answer, out), clean_finish)
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
