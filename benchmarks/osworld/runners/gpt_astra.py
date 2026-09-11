"""OSWorld Verified replica driven by GPT Astra through the Codex CLI.

The environment, MCP action surface and official evaluator are identical to agent_computer;
only the model/agent runtime changes. Results live under ``agent_computer_astra``.
"""
import json
import hashlib
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config, tasks
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners.agent_computer import (
    _annotate_incidental, _bounded, _capture_eval_state, _environment_error_rec,
    _provenance, _score,
)
from core import results as results_io
from core.agent_loop import extract_answer, preview
from core.codex_loop import (
    ALLOWED_MCP_TOOLS, APPROVAL_MODE, DISABLED_FEATURES, build_codex_cmd, run_codex_meta,
    session_context,
)

_LOCK = Path(__file__).resolve().parents[1] / "astra_campaign_lock.json"
_SUSPICIOUS = ("evaluator", "gold", "results", "task_spec")


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
            or tuple(policy["allowed_mcp_tools"]) != ALLOWED_MCP_TOOLS):
        raise SystemExit("Astra tool policy differs from the frozen campaign lock")


def preflight():
    _validate_campaign_lock()
    # core.run writes harness.json after preflight; materialize defaults so a direct invocation
    # records the same campaign identity as the shell driver.
    os.environ.setdefault("OSW_ASTRA_MODEL", config.ASTRA_MODEL)
    os.environ.setdefault("OSW_ASTRA_REASONING_EFFORT", config.ASTRA_REASONING_EFFORT)
    os.environ.setdefault("OSW_ASTRA_CODEX_VERSION", config.ASTRA_CODEX_VERSION)
    binary = shutil.which("codex")
    if not binary:
        raise SystemExit("codex CLI not found on PATH; install/login before running GPT Astra")
    try:
        version_text = subprocess.run(
            [binary, "--version"], check=True, capture_output=True, text=True, timeout=15
        ).stdout.strip()
        actual_version = version_text.rsplit(" ", 1)[-1]
        if config.ASTRA_CODEX_VERSION and actual_version != config.ASTRA_CODEX_VERSION:
            raise SystemExit(
                f"codex CLI version mismatch: campaign pins {config.ASTRA_CODEX_VERSION}, "
                f"found {actual_version} ({version_text})"
            )
        login = subprocess.run(
            [binary, "login", "status"], check=True, capture_output=True, text=True, timeout=15
        )
        if "logged in" not in (login.stdout + login.stderr).lower():
            raise SystemExit("codex CLI is not logged in")
        catalog_raw = subprocess.run(
            [binary, "debug", "models"], check=True, capture_output=True, text=True, timeout=30
        ).stdout
        catalog = json.loads(catalog_raw)
        model = next((m for m in catalog.get("models", [])
                      if m.get("slug") == config.ASTRA_MODEL), None)
        if not model:
            raise SystemExit(f"{config.ASTRA_MODEL!r} is absent from the Codex model catalog")
        supported = {x.get("effort") for x in model.get("supported_reasoning_levels", [])}
        if config.ASTRA_REASONING_EFFORT not in supported:
            raise SystemExit(
                f"reasoning effort {config.ASTRA_REASONING_EFFORT!r} is unsupported for "
                f"{config.ASTRA_MODEL}; supported={sorted(supported)}"
            )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise SystemExit(f"codex CLI preflight failed: {exc}") from exc


def _telemetry(meta, stderr=""):
    usage = meta.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cached_tokens = usage.get("cached_input_tokens")
    if cached_tokens is None:
        cached_tokens = input_details.get("cached_tokens")
    reasoning_tokens = usage.get("reasoning_output_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = output_details.get("reasoning_tokens")
    return {
        "agent_clean_finish": None,
        "agent_runtime": "codex_cli",
        "agent_num_turns": meta.get("num_turns"),
        "agent_stop_reason": "timeout" if meta.get("timed_out") else
                             ("error" if meta.get("is_error") else "completed"),
        "agent_is_error": meta.get("is_error"),
        "agent_errors": meta.get("errors"),
        "agent_api_error_status": _api_error_status(meta, stderr),
        # Codex JSONL does not report the user's actual charge (ChatGPT pooled usage versus API
        # billing differ), so never label a token-derived estimate as billed cost.
        "agent_cost_usd": usage.get("cost_usd") or usage.get("total_cost_usd"),
        "agent_estimated_api_cost_usd": _estimated_api_cost(
            input_tokens, cached_tokens, output_tokens),
        "agent_cost_basis": "2026-09-09 list price: $10/M input, $1/M cached, $50/M output",
        "agent_input_tokens": input_tokens,
        "agent_output_tokens": output_tokens,
        "agent_cached_input_tokens": cached_tokens,
        "agent_reasoning_output_tokens": reasoning_tokens,
        "agent_tool_calls": meta.get("tool_calls"),
        "agent_mcp_tool_calls": meta.get("mcp_tool_calls"),
        "agent_non_mcp_tool_calls": meta.get("non_mcp_tool_calls"),
        "agent_tool_names": meta.get("tool_names"),
        "agent_session_id": meta.get("session_id"),
        "agent_returncode": meta.get("returncode"),
    }


def _estimated_api_cost(input_tokens, cached_tokens, output_tokens):
    if input_tokens is None or output_tokens is None:
        return None
    cached = cached_tokens or 0
    uncached = max(0, input_tokens - cached)
    return round((uncached * 10 + cached * 1 + output_tokens * 50) / 1_000_000, 6)


def _api_error_status(meta, stderr=""):
    """Codex does not always put a usage limit in the JSONL error stream -- it can land only on
    stderr. Missing it would let a throttled run be scored as if the agent had had its chance."""
    text = (json.dumps(meta.get("errors") or "") + " " + (stderr or "")).lower()
    if "429" in text or "rate limit" in text or "usage limit" in text or "quota" in text:
        return 429
    return None


def _provenance_astra(task, ctrl, started_at, codex_version, session_id=None):
    rec = _provenance(task, ctrl, started_at)
    # `codex exec --json` names no model anywhere in its event stream, so the request is
    # unverified on its own. Codex's own rollout file does record what it applied -- model,
    # reasoning effort, approval and sandbox policy -- so read the receipt instead of trusting
    # the flag. Unverifiable stays None: this is the field the Sonnet campaign lacked when it
    # silently spanned three models (config.py::MODEL).
    served = session_context(session_id) if session_id else {}
    served_model = served.get("model_served")
    served_effort = served.get("reasoning_effort_served")
    rec.update({
        "model_requested": config.ASTRA_MODEL,
        "model_served": served_model,
        "model_pinned": True,
        "model_mismatch": (served_model != config.ASTRA_MODEL) if served_model else None,
        "reasoning_effort_mismatch": (
            served_effort != config.ASTRA_REASONING_EFFORT) if served_effort else None,
        **{k: v for k, v in served.items() if k != "model_served"},
        "agent_runtime": "codex_cli",
        "agent_runtime_version": codex_version,
        "reasoning_effort": config.ASTRA_REASONING_EFFORT or None,
        # Codex has no Claude-style --max-turns flag; the shared wall-clock and the prompt's
        # action budget are the applicable controls for this runner.
        "max_turns": None,
        "max_steps": config.MAX_STEPS,
    })
    return rec


def _codex_version():
    try:
        return subprocess.run(["codex", "--version"], check=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return None


def _rate_limit_rec(task, status):
    return {
        "id": task["id"], "outcome": "RATE_LIMITED", "error_type": "APIError",
        "error": f"api_error_status={status}",
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _write_tool_audit(out, events):
    """Persist each provider-reported tool call and flag possible oracle reads.

    Codex event schemas evolve, so arguments/output are retained from every known field rather
    than inferred from tool names.  Missing fields stay null and are therefore auditable too.
    """
    calls, evidence = [], []
    for event in events or []:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        kind = item.get("type", "")
        if not (kind.endswith("_call") or kind in {"command_execution", "web_search"}):
            continue
        args = item.get("arguments", item.get("input", item.get("command")))
        outcome = item.get("output", item.get("result", item.get("error")))
        # Tool output can contain screenshots (base64 megabytes). The audit records outcome,
        # not a duplicate visual artifact; final.png and conversation.jsonl retain the bytes.
        if isinstance(outcome, dict) and isinstance(outcome.get("content"), list):
            parts = outcome["content"]
            outcome = {"content_types": [part.get("type") for part in parts
                                         if isinstance(part, dict)],
                       "success": True}
        record = {"type": item.get("tool") or item.get("name") or kind,
                  "arguments": args, "outcome": outcome,
                  "event": event.get("type")}
        calls.append(record)
        haystack = json.dumps(record, ensure_ascii=False).lower()
        hits = [needle for needle in _SUSPICIOUS if needle in haystack]
        if hits:
            evidence.append({"tool": record["type"], "needles": hits,
                             "arguments": args})
    audit = {"tool_calls": calls, "contaminated": bool(evidence), "evidence": evidence}
    (out / "tool_audit.json").write_text(json.dumps(audit, indent=2))
    if evidence:
        (out / "contamination.json").write_text(json.dumps(audit, indent=2))
    return audit


def run(task, *, env, out, refs=None, dry=False):
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

    workdir = Path(tempfile.mkdtemp(prefix="osw-astra-"))
    cmd = build_codex_cmd(
        agent_prompt(task), model=config.ASTRA_MODEL, cwd=workdir,
        controller_url=controller_url, reasoning_effort=config.ASTRA_REASONING_EFFORT or None,
    )
    if dry:
        print("DRY-RUN command:\n ", preview(cmd))
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    try:
        meta = run_codex_meta(cmd, timeout=config.TASK_TIMEOUT)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Preserve the complete provider trajectory; unlike Claude Code this is emitted directly by
    # the headless invocation, so no lookup in a user-global session directory is necessary.
    transcript = out / "conversation.jsonl"
    transcript.write_text(meta.pop("raw", ""))
    stderr = meta.pop("stderr", "")
    if stderr:
        (out / "codex_stderr.txt").write_text(stderr)
    audit = _write_tool_audit(out, meta.pop("events", None))
    text = meta.get("result", "")
    answer = extract_answer(text)
    clean_finish = bool(answer) and not meta.get("is_error", False)
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
             "contamination_evidence": audit["evidence"]}

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

    if meta.get("non_mcp_tool_calls"):
        results_io.write_result(out, {
            "id": task["id"], "bucket": tasks.bucket_of(task),
            "instruction": task["instruction"], "answer": answer,
            "provenance": provenance, **trace, **telemetry,
        })
        results_io.write_infra_error(out, {
            "id": task["id"], "outcome": "HARNESS_ERROR",
            "error_type": "ToolSurfaceViolation",
            "error": f"Codex used non-MCP tools: {meta.get('tool_names')}",
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return ""

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
    })
    results_io.write_eval(out, {"id": task["id"], **rec})
    return answer
