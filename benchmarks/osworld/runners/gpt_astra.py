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

from benchmarks.osworld import config, tasks
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners import astra_common
from benchmarks.osworld.runners.agent_computer import (
    _annotate_incidental, _bounded, _capture_eval_state, _environment_error_rec,
    _provenance, _score,
)
from core import results as results_io
from core.agent_loop import extract_answer, preview
from core.codex_loop import (
    ALLOWED_MCP_TOOLS, APPROVAL_MODE, DISABLED_FEATURES, build_codex_cmd, run_codex_meta,
)

_LOCK = Path(__file__).resolve().parents[1] / "astra_campaign_lock.json"
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
            or tuple(policy["allowed_mcp_tools"]) != ALLOWED_MCP_TOOLS):
        raise SystemExit("Astra tool policy differs from the frozen campaign lock")


def preflight():
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
