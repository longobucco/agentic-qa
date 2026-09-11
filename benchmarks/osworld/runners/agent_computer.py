"""OSWorld runner: claude -p + the OSWorld MCP drive the desktop, then score with OSWorld's
official evaluators while the desktop is still live (self_eval) and write eval.json. Falls back
to the offline checker in evaluate.py when desktop_env isn't importable.

Shared, policy-free primitives (MCP config, telemetry, provenance, the post-run watchdog,
transcript capture, scoring) live in runners/common.py, reused unchanged by
runners/verify_replan.py (docs/verify-replan-minimal-integration-plan.md). Re-imported here by
name so every existing external reference to e.g. `agent_computer._mcp_config` keeps resolving
-- see benchmarks/osworld/tests/test_runner.py's characterization tests for the frozen contract.
"""
import os
import re

from datetime import datetime, timezone

from benchmarks.osworld import config, tasks
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners.common import (
    OSWORLD_TOOLS, _action_history, _agent_telemetry, _annotate_incidental, _bounded,
    _capture_eval_state, _clean_finish, _environment_error_rec, _evaluate_with_retry,
    _evaluator_provenance, _mcp_config, _model_mismatch, _POST_RUN_TIMEOUT_S, _provenance,
    _rate_limit_infra_rec, _rate_limit_result_rec, _save_conversation_transcript, _score,
    _served_by, _a11y_health,
)
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude_meta
from core import results as results_io


# Grounding harness (config.GROUNDING, docs/grounding-harness-plan.md). Names must match what
# mcp/grounding_tools.register() attaches to the `osworld` FastMCP instance, prefix included --
# a wrong prefix here is silent under --dangerously-skip-permissions (which makes --allowedTools
# pre-approval rather than a gate), which is exactly how the same mistake went unnoticed in
# runners/verify_replan.py's READONLY_TOOLS for several commits.
GROUNDING_TOOLS = [
    "mcp__osworld__find_element", "mcp__osworld__click_element", "mcp__osworld__list_elements",
]


def _allowed_tools():
    """The baseline OSWorld toolset, plus the grounding arm's tools when that arm is on.

    Kept here rather than in common.OSWORLD_TOOLS on purpose: that module's extraction discipline
    is that it reads no G5-arm config knob (see its docstring), and GROUNDING is one.
    """
    return OSWORLD_TOOLS + (GROUNDING_TOOLS if config.GROUNDING else [])


def _grounding_precheck(ctrl):
    """Refuse to spend a grounding run against a dead accessibility channel.

    Only when the arm is ON: the baseline does not depend on a11y (the agent works from
    screenshots), so an empty tree is a recorded fact there, not a reason to fail a run. With the
    arm on it IS the reason -- every find_element/click_element would resolve nothing and the run
    would look like a null result for the mechanism rather than a broken environment. That
    confusion already cost this project a whole analysis: the channel was empty on all 456
    captures across every campaign and nothing on disk said so.

    Returns an error string to abort with, or None to proceed.
    """
    if not config.GROUNDING:
        return None
    health = _a11y_health(ctrl)
    if health.get("a11y_ok"):
        return None
    return (f"grounding arm requested but the accessibility channel is not reporting "
            f"({health.get('a11y_reason')}); nodes={health.get('a11y_nodes')}. Rebuild the guest "
            f"image with the AT-SPI bus from docker/start.sh and re-pin config.IMAGE, or unset "
            f"OSW_GROUNDING -- see docs/grounding-harness-plan.md Section 8.")


def _grounding_telemetry():
    """Per-run record of the grounding arm's settings, merged into result.json.

    The results tree is already suffixed when the arm is on (config.SYSTEM_NAME), but a directory
    name records only that the arm ran, not how it was configured -- and GROUNDING_MIN_SCORE
    changes what resolves, so two runs under the same tree are not comparable without it.
    """
    if not config.GROUNDING:
        return {"grounding_used": False}
    return {
        "grounding_used": True,
        "grounding_verify": config.GROUNDING_VERIFY,
        "grounding_min_score": config.GROUNDING_MIN_SCORE,
    }


def _extra_flags():
    """Extra claude CLI flags for the G5 tool-restriction arms (config.ENFORCE_SANDBOX, idea
    #10; config.RESTRICT_RUN_PYTHON, idea #11). Both use --disallowedTools, a real deny list
    (unlike --allowedTools, which only suppresses the confirmation prompt without restricting
    availability -- see ENFORCE_SANDBOX's own comment in config.py) -- combined into one flag
    list rather than two separate ones so --strict-mcp-config appears at most once."""
    disallowed = []
    if config.ENFORCE_SANDBOX:
        disallowed += ["Bash", "WebSearch", "WebFetch"]
    if config.RESTRICT_RUN_PYTHON:
        disallowed += ["mcp__osworld__run_python"]
    if not disallowed:
        return None
    return ["--disallowedTools", *disallowed, "--strict-mcp-config"]


def _implies_done(answer):
    """Same DONE/FAIL split as _action_history, exposed on its own: the in-loop verifier only
    engages when the agent claims completion -- a self-reported FAIL is already the agent's own
    admission and needs no independent check to act on."""
    a = (answer or "").strip().upper()
    return bool(a) and not (a.startswith("FAIL") or "INFEASIBLE" in a)


# Idea #15 pilot (2026-09-09, 6 tasks / 18 runs, docs/g5-arm-inloop-verify-pilot -- see
# analysis): the generic nudge below ("does not confirm... check again") never once changed the
# agent's self-report (0/11 retries). Reading the retried transcripts explained why: the verifier
# is screenshot-only, so it is structurally blind to any success criterion that isn't visible in
# a screenshot (audio volume, on-disk file contents, background config keys) -- and the agent,
# which DOES have run_python to check those directly, reasonably discounts a same-generic
# disagreement it can explain away ("the review saw a black screen because I'd closed my
# terminal", etc.). The fix tried here is NOT giving the verifier more tools (that would reopen
# the oracle-contamination risk documented in the project doc's Section 6 -- a verifier that can
# read files/run code can read the same evaluator/gold sources the agent did) -- it is making the
# verifier's disagreement specific enough that a generic "must be non-visual" dismissal doesn't
# trivially apply.
_INLOOP_VERIFIER_PROMPT = """Read the image file at the EXACT absolute path: {path} -- it is a \
screenshot of a Linux desktop, taken at the end of an attempt to complete a task. Do not search \
for anything else; the path given is correct and complete.

The task instruction was: {instruction}

Based ONLY on what you see in the screenshot, does the desktop state satisfy this instruction? \
Answer with exactly two lines:
ANSWER: DONE or ANSWER: FAIL
REASON: one brief sentence naming the SPECIFIC element, text, or state that is present, \
missing, or wrong -- not a generic restatement of the instruction."""

_INLOOP_RETRY_PROMPT = """An independent review of the current screen does not confirm the \
task is complete. The task instruction was: {instruction}

Specifically, the review said: {reason}

Address that specific point. If the task is not actually finished, continue working until it \
genuinely is, then give your final answer in exactly the same format as before: 'ANSWER: DONE' \
or 'ANSWER: FAIL'. If you are confident the task truly is already complete despite this specific \
concern, explain concretely why that exact point is wrong or not applicable, then repeat \
'ANSWER: DONE'."""

_REASON_RE = re.compile(r"^REASON:\s*(.*)$", re.MULTILINE)


def _verify_with_reason(png_path, instruction, *, timeout=120):
    """Same mechanism as g5_verifier_check.verify() (fresh context, screenshot + instruction
    only, Read-only) but kept as its own call rather than reusing that function, so tightening
    the in-loop prompt here can never change what #9/#12 measure or how they reproduce."""
    prompt = _INLOOP_VERIFIER_PROMPT.format(path=png_path, instruction=instruction)
    cmd = build_claude_cmd(prompt, max_turns=4, allowed_tools=["Read"], model=config.MODEL or None)
    meta = run_claude_meta(cmd, timeout=timeout)
    text = meta.get("result", "")
    m = _REASON_RE.search(text)
    return {"answer": extract_answer(text), "reason": m.group(1).strip() if m else None,
            "raw": text, "model_served": _served_by(meta)}


def _inloop_verify(ctrl, task, answer, meta, mcp_config_path, out):
    """G5 idea #15: on a self-reported DONE, run an independent check (fresh context, screenshot
    only, no memory of the attempt) while the desktop is still live, and on disagreement give the
    agent one real follow-up turn via --resume, naming the verifier's specific reason rather than
    a generic "check again" (see the pilot post-mortem above). Returns (answer, meta,
    extra_telemetry) -- unchanged from the inputs whenever the mechanism is off, doesn't apply, or
    errors, so a failure here can never break an otherwise normal run.
    """
    if not config.INLOOP_VERIFY or not _implies_done(answer):
        return answer, meta, {"inloop_verify_used": False}
    if tasks.app_of(task) in config.INLOOP_VERIFY_SKIP_APPS:
        return answer, meta, {"inloop_verify_used": False, "inloop_verify_skipped_app": True}
    try:
        png_path = out / "inloop_pre_verify.png"
        png_path.write_bytes(ctrl.screenshot())
        v = _verify_with_reason(png_path.resolve(), task.get("instruction", ""))
    except Exception as e:
        print(f"[osworld] in-loop verify skipped (capture/verify failed): {e}")
        return answer, meta, {"inloop_verify_used": False, "inloop_verify_error": str(e)}

    base = {
        "inloop_verify_used": True,
        "inloop_verify_verdict": v["answer"],
        "inloop_verify_reason": v["reason"],
        "inloop_verify_model": v["model_served"],
        "inloop_verify_first_answer": answer,
        "inloop_verify_retried": False,
    }
    if (v["answer"] or "").strip().upper().startswith("DONE"):
        return answer, meta, base   # verifier agrees -- no retry

    session_id = meta.get("session_id")
    if not session_id:
        return answer, meta, {**base, "inloop_verify_error": "no session_id to resume"}
    reason = v["reason"] or "the current screen does not appear to show the task as complete"
    try:
        cmd2 = build_claude_cmd(
            _INLOOP_RETRY_PROMPT.format(instruction=task.get("instruction", ""), reason=reason),
            model=config.MODEL or None, max_turns=config.INLOOP_VERIFY_MAX_TURNS,
            mcp_config=mcp_config_path, allowed_tools=_allowed_tools(), resume=session_id,
        )
        meta2 = run_claude_meta(cmd2, timeout=config.TASK_TIMEOUT)
    except Exception as e:
        print(f"[osworld] in-loop verify retry failed: {e}")
        return answer, meta, {**base, "inloop_verify_error": f"retry call failed: {e}"}

    # Two real API calls were made regardless of whether the second produced a usable answer --
    # cost/turns/duration must sum both, or a retry that changes nothing still silently
    # under-reports what it spent (the same species of gap _agent_telemetry was written to
    # close for the outer call).
    summed = {
        "total_cost_usd": (meta.get("total_cost_usd") or 0) + (meta2.get("total_cost_usd") or 0),
        "num_turns": (meta.get("num_turns") or 0) + (meta2.get("num_turns") or 0),
        "duration_ms": (meta.get("duration_ms") or 0) + (meta2.get("duration_ms") or 0),
        "duration_api_ms": (meta.get("duration_api_ms") or 0) + (meta2.get("duration_api_ms") or 0),
    }
    answer2 = extract_answer(meta2.get("result", ""))
    if not answer2:
        # resumed call produced nothing usable -- keep the original answer text, but the spend
        # was real either way, so still fold it into what gets recorded.
        return answer, {**meta, **summed}, {**base, "inloop_verify_retried": True,
                                            "inloop_verify_second_answer": None}
    return answer2, {**meta2, **summed}, {**base, "inloop_verify_retried": True,
                                          "inloop_verify_second_answer": answer2}


def run(task, *, env, out, refs=None, dry=False):
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    controller_url = ctrl.base_url if ctrl else config.CONTROLLER_URL

    setup_error = getattr(env, "setup_error", None)
    if setup_error and not dry:
        rec = _environment_error_rec(task, setup_error)
        rec["result"]["provenance"] = _provenance(task, ctrl, started_at)
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""

    # Before building the command: an arm whose channel is dead produces meaningless data, and
    # the failure has to be loud rather than look like a null result (see _grounding_precheck).
    grounding_block = _grounding_precheck(ctrl) if not dry else None
    if grounding_block:
        rec = _environment_error_rec(task, grounding_block)
        rec["result"]["provenance"] = _provenance(task, ctrl, started_at)
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""

    mcp_config_path = _mcp_config(controller_url)
    cmd = build_claude_cmd(
        agent_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        mcp_config=mcp_config_path,
        allowed_tools=_allowed_tools(),
        extra=_extra_flags(),
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        os.unlink(mcp_config_path)
        return None

    inloop_telemetry = {}
    try:
        meta = run_claude_meta(cmd, timeout=config.TASK_TIMEOUT)
        # Kept alive past this first call, on purpose: idea #15's follow-up turn (below) needs
        # the SAME --mcp-config to --resume this session with the OSWorld tools still available.
        # Deleting it right after the first call (as this used to) would make any retry attempt
        # silently lose desktop control -- moved into the same try/finally that now spans both
        # calls so the file outlives whichever one actually happens.
        if ctrl and not meta.get("api_error_status"):
            first_answer = extract_answer(meta.get("result", ""))
            # the returned answer is discarded here -- `meta` (possibly the resumed call's
            # envelope) is re-extracted the same way as any other run just below, so there is
            # exactly one place that turns a `meta` into the recorded `answer`.
            _, meta, inloop_telemetry = _inloop_verify(
                ctrl, task, first_answer, meta, mcp_config_path, out)
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
        result_rec["provenance"] = _provenance(task, ctrl, started_at)
        # capture BEFORE write_result so the transcript status rides in the record rather
        # than being lost -- a rate-limited run legitimately has no tool calls, so its
        # transcript is a stub, and only this flag distinguishes "stub because throttled"
        # from "transcript failed to copy".
        result_rec.update(_save_conversation_transcript(meta, out, task["id"]))
        results_io.write_result(out, result_rec)
        results_io.write_infra_error(out, _rate_limit_infra_rec(task, api_error_status))
        return ""

    text = meta.get("result", "")
    answer = extract_answer(text)
    clean_finish = _clean_finish(meta, answer)
    results_io.write_output(out, text)
    transcript = _save_conversation_transcript(meta, out, task["id"])

    eval_state = _bounded("eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    telemetry = _agent_telemetry(meta)
    telemetry["agent_clean_finish"] = clean_finish
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"],
        "answer": answer,
        "eval_state": eval_state,
        "provenance": _provenance(task, ctrl, started_at),
        **transcript,
        **_model_mismatch(meta),
        **telemetry,
        **inloop_telemetry,
        **_grounding_telemetry(),
        **_a11y_health(ctrl),
    })
    rec = _annotate_incidental(_bounded("scoring", _score, ctrl, task, answer, out), clean_finish)
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
