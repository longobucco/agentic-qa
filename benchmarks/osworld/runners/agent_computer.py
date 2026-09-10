"""OSWorld runner: claude -p + the OSWorld MCP drive the desktop, then score with OSWorld's
official evaluators while the desktop is still live (self_eval) and write eval.json. Falls back
to the offline checker in evaluate.py when desktop_env isn't importable.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import requests

from benchmarks.osworld import config, evaluate, tasks
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.prompts import agent_prompt
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude_meta
from core import results as results_io

OSWORLD_TOOLS = [
    "mcp__osworld__screenshot", "mcp__osworld__a11y_tree",
    "mcp__osworld__click", "mcp__osworld__double_click", "mcp__osworld__right_click",
    "mcp__osworld__move", "mcp__osworld__scroll", "mcp__osworld__type",
    "mcp__osworld__key", "mcp__osworld__run_python", "mcp__osworld__wait",
]


def _mcp_config(controller_url):
    spec = {
        "type": "stdio", "command": "python",
        "args": ["-m", "benchmarks.osworld.mcp.server"],
        "env": {"OSW_CONTROLLER_URL": controller_url or ""},
    }
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mcpServers": {"osworld": spec}}, f)
    f.close()
    return f.name


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
        "image": config.IMAGE,
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
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


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
            mcp_config=mcp_config_path, allowed_tools=OSWORLD_TOOLS, resume=session_id,
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


def _score(ctrl, task, answer, out):
    url = ctrl.base_url if ctrl else config.CONTROLLER_URL
    reward, err = None, None
    # own the cache dir so the downloaded gold can be hashed afterwards
    # (see osworld_eval.hash_gold_artifacts)
    gold_dir = tempfile.mkdtemp(prefix="osw_gold_")
    gold_sha256 = {}
    if url:
        try:
            reward = _evaluate_with_retry(url, task, _action_history(answer), gold_dir)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"   # desktop unreachable / getter failed -> fall back
        finally:
            # scoring can fail after the gold was already fetched -- still worth recording
            gold_sha256 = osworld_eval.hash_gold_artifacts(gold_dir, task)
            # hashed, no longer needed -- an unattended multi-day campaign otherwise leaks one
            # of these per run (found live 2026-08-16: 5400 stray temp dirs/files, 3.6GB, after
            # ~1000 run attempts -- see the matching cleanup for _mcp_config below)
            shutil.rmtree(gold_dir, ignore_errors=True)
    if reward is not None:   # OSWorld's official evaluators
        return {"verdict": osworld_eval.reward_to_verdict(reward), "reward": reward,
                "reason": f"official {task.get('evaluator', {}).get('func')} -> {reward:.2f}",
                "source": "official", "gold_sha256": gold_sha256}
    # fallback: no verified way to score without the official evaluator (see evaluate.py) --
    # always EVAL_ERROR, kept only as a diagnostic of whatever state we did capture.
    rec = {**evaluate.osworld_check(task, answer, None, out), "source": "offline_fallback",
           "gold_sha256": gold_sha256}
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

    mcp_config_path = _mcp_config(controller_url)
    cmd = build_claude_cmd(
        agent_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        mcp_config=mcp_config_path,
        allowed_tools=OSWORLD_TOOLS,
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
        # live 2026-08-16 -- see the matching cleanup on gold_dir in _score).
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
    })
    rec = _annotate_incidental(_bounded("scoring", _score, ctrl, task, answer, out), clean_finish)
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
