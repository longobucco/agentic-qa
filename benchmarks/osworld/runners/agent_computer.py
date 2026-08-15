"""OSWorld runner: claude -p + the OSWorld MCP drive the desktop, then score with OSWorld's
official evaluators while the desktop is still live (self_eval) and write eval.json. Falls back
to the offline checker in evaluate.py when desktop_env isn't importable.
"""
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone

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


def _provenance(task, ctrl, started_at):
    """Per-run pinning record. harness.json gets overwritten by the next invocation; this
    rides with the individual run so the record stays self-describing after reconfiguration."""
    return {
        "task_sha256": hashlib.sha256(
            json.dumps(task, sort_keys=True).encode()).hexdigest(),
        "image": config.IMAGE,
        "controller_url": getattr(ctrl, "base_url", None) or config.CONTROLLER_URL or None,
        "release": config.RELEASE,
        "max_turns": config.MAX_TURNS,
        "task_timeout": config.TASK_TIMEOUT,
        "observation": config.OBSERVATION,
        "action_space": config.ACTION_SPACE,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


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


_EVAL_RETRY_ATTEMPTS = 3
_EVAL_RETRY_DELAY_S = 5


def _evaluate_with_retry(url, task, action_history, cache_dir):
    """Retry the official evaluator on a bare connection reset. Observed live, often
    (>1/3 of scoring attempts in the first G3 batch): requests.exceptions.ConnectionError
    wrapping ConnectionResetError(54, ...) against the Daytona proxy, always right after a
    long agent session -- looks like proxy instability under sustained load, not a
    deterministic evaluator failure, so it's worth a few retries before falling back."""
    last_err = None
    for attempt in range(_EVAL_RETRY_ATTEMPTS):
        try:
            return osworld_eval.evaluate_official(url, task, action_history, cache_dir=cache_dir)
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt < _EVAL_RETRY_ATTEMPTS - 1:
                time.sleep(_EVAL_RETRY_DELAY_S)
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
    return {"id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": "", **telemetry}


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

    cmd = build_claude_cmd(
        agent_prompt(task),
        model=config.MODEL or None,
        max_turns=config.MAX_TURNS,
        mcp_config=_mcp_config(controller_url),
        allowed_tools=OSWORLD_TOOLS,
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        return None

    meta = run_claude_meta(cmd, timeout=config.TASK_TIMEOUT)
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
        results_io.write_result(out, result_rec)
        results_io.write_infra_error(out, _rate_limit_infra_rec(task, api_error_status))
        return ""

    text = meta.get("result", "")
    answer = extract_answer(text)
    clean_finish = _clean_finish(meta, answer)
    results_io.write_output(out, text)

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
        **telemetry,
    })
    rec = _annotate_incidental(_bounded("scoring", _score, ctrl, task, answer, out), clean_finish)
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
