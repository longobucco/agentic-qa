"""Verify-Replan runner (docs/verify-replan-minimal-integration-plan.md): a second, separate
OSWorld system-under-test alongside agent_computer.py -- never imported BY it, never changing
its behavior. Same environment, same MCP action surface for the acting roles, same official
evaluator, scored once at the end while the same VM is still live.

    Execute -> Verify -> [Done | Replan -> Execute recovery -> Verify]

Reuses runners/common.py's policy-free primitives (MCP config, telemetry, provenance, the
post-run watchdog, transcript capture, scoring) and benchmarks/osworld/verification.py's pure
claim/audit parsing and recovery decision. Interpretation note on Section 11's audit-parser-retry
row: rather than a harness-level retry call, the Auditor's own multi-turn budget
(config.VR_AUDITOR_MAX_TURNS) is the retry surface -- its prompt already demands exactly one
JSON block: a session that still fails to produce one after its full turn budget is scored
AUDIT_HARNESS_ERROR (no recovery), not given a second harness-orchestrated attempt.
"""
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config, tasks, verification
from benchmarks.osworld.prompts import agent_prompt
from benchmarks.osworld.runners.common import (
    OSWORLD_TOOLS, _agent_telemetry, _annotate_incidental, _bounded, _capture_eval_state,
    _clean_finish, _environment_error_rec, _mcp_config, _model_mismatch, _provenance,
    _rate_limit_infra_rec, _rate_limit_result_rec, _save_conversation_transcript, _score,
)
from core.agent_loop import build_claude_cmd, extract_answer, preview, run_claude_meta
from core import results as results_io

READONLY_TOOLS = ["mcp__osworld__screenshot", "mcp__osworld__a11y_tree", "mcp__osworld__wait"]


def _readonly_mcp_config(controller_url):
    """Same shape as common._mcp_config but launches readonly_server.py instead of server.py --
    the Auditor's actual protocol-level boundary (see mcp/readonly_server.py's own docstring and
    tests/test_readonly_mcp.py; a prompt telling the model not to act is not this boundary)."""
    spec = {
        "type": "stdio", "command": "python",
        "args": ["-m", "benchmarks.osworld.mcp.readonly_server"],
        "env": {"OSW_CONTROLLER_URL": controller_url or ""},
    }
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mcpServers": {"osworld_readonly": spec}}, f)
    f.close()
    return f.name


def _role_result(meta, role):
    telemetry = _agent_telemetry(meta)
    telemetry.update({f"role_{k}" if not k.startswith("agent_") else k: v
                      for k, v in _model_mismatch(meta).items()})
    return {"role": role, **telemetry}


def _sum(role_usage, field):
    return sum((r.get(field) or 0) for r in role_usage.values())


def _save_role_transcript(meta, role_dir, task_id, role):
    role_dir.mkdir(parents=True, exist_ok=True)
    return _save_conversation_transcript(meta, role_dir, f"{task_id}:{role}")


def _timeline_event(timeline, phase, **fields):
    timeline.append({"phase": phase, "at": datetime.now(timezone.utc).isoformat(), **fields})


def run(task, *, env, out, refs=None, dry=False):
    started_at = datetime.now(timezone.utc).isoformat()
    ctrl = getattr(env, "browser", None)
    controller_url = ctrl.base_url if ctrl else config.CONTROLLER_URL

    setup_error = getattr(env, "setup_error", None)
    if setup_error and not dry:
        rec = _environment_error_rec(task, setup_error)
        rec["result"]["provenance"] = _provenance(task, ctrl, started_at)
        rec["result"]["harness"] = "verify_replan"
        results_io.write_result(out, rec["result"])
        results_io.write_eval(out, rec["eval"])
        return ""

    vr_dir = out / "verify_replan"
    vr_dir.mkdir(parents=True, exist_ok=True)
    timeline = []
    role_usage = {}

    full_mcp_path = _readonly_mcp_path = None
    prompt = agent_prompt(task) + verification.EXECUTOR_CLAIM_CONTRACT_SUFFIX
    full_mcp_path = _mcp_config(controller_url)
    cmd_initial = build_claude_cmd(
        prompt, model=config.MODEL or None, max_turns=config.VR_INITIAL_MAX_TURNS,
        mcp_config=full_mcp_path, allowed_tools=OSWORLD_TOOLS,
    )
    if dry:
        print("DRY-RUN command (initial executor):\n ", preview(cmd_initial))
        os.unlink(full_mcp_path)
        return None

    deadline = time.monotonic() + config.VR_TOTAL_TIMEOUT

    try:
        _timeline_event(timeline, "EXECUTE_INITIAL")
        meta_initial = run_claude_meta(cmd_initial, timeout=config.VR_INITIAL_TIMEOUT)

        if meta_initial.get("api_error_status"):
            # Section 11: rate-limited before any action -> RATE_LIMITED, no eval.json, same
            # infra-flake semantics as agent_computer's own rate-limit path.
            _timeline_event(timeline, "INFRA_ERROR", role="initial")
            result_rec = _rate_limit_result_rec(task, meta_initial)
            result_rec["provenance"] = _provenance(task, ctrl, started_at)
            result_rec["harness"] = "verify_replan"
            result_rec.update(_save_conversation_transcript(meta_initial, out, task["id"]))
            results_io.write_result(out, result_rec)
            results_io.write_infra_error(
                out, _rate_limit_infra_rec(task, meta_initial["api_error_status"]))
            return ""

        initial_dir = vr_dir / "initial"
        initial_dir.mkdir(parents=True, exist_ok=True)
        text_initial = meta_initial.get("result", "")
        answer_initial = extract_answer(text_initial)
        claim_initial = verification.parse_executor_claim(text_initial)
        (initial_dir / "output.txt").write_text(text_initial)
        (initial_dir / "claim.json").write_text(json.dumps(claim_initial, indent=2))
        _save_role_transcript(meta_initial, initial_dir, task["id"], "initial")
        role_usage["initial"] = _role_result(meta_initial, "initial")
        _timeline_event(timeline, "EXECUTE_INITIAL_DONE", claim_status=claim_initial["status"])

        final_answer, final_meta = answer_initial, meta_initial
        audit_final = None
        recovery_triggered = False
        recovery_attempts = 0

        if ctrl is None:
            # No live desktop to audit against (should not happen outside test doubles) -- fall
            # straight through to scoring on the initial executor's own answer.
            audit_initial = None
        else:
            _readonly_mcp_path = _readonly_mcp_config(controller_url)
            audit_dir = vr_dir / "audit_1"
            audit_dir.mkdir(parents=True, exist_ok=True)
            _timeline_event(timeline, "AUDIT_1")
            cmd_audit = build_claude_cmd(
                verification.render_auditor_prompt(task, claim_initial),
                model=config.VR_AUDITOR_MODEL or config.MODEL or None,
                max_turns=config.VR_AUDITOR_MAX_TURNS,
                mcp_config=_readonly_mcp_path, allowed_tools=READONLY_TOOLS,
            )
            meta_audit1 = run_claude_meta(cmd_audit, timeout=config.VR_AUDIT_TIMEOUT)
            if meta_audit1.get("api_error_status"):
                # Section 11: AUDIT_INFRA_ERROR -- score without recovery, on a valid desktop
                # state that the harness just never got an opinion on.
                audit_initial = verification.audit_error("auditor rate-limited")
                _timeline_event(timeline, "AUDIT_INFRA_ERROR", role="audit_1")
            else:
                audit_text1 = meta_audit1.get("result", "")
                audit_initial = verification.parse_audit_report(audit_text1)
                (audit_dir / "report.json").write_text(json.dumps(audit_initial, indent=2))
                _save_role_transcript(meta_audit1, audit_dir, task["id"], "audit_1")
                role_usage["audit_1"] = _role_result(meta_audit1, "audit_1")
            _timeline_event(timeline, "AUDIT_1_DONE", verdict=audit_initial["verdict"])

            budget_remaining = deadline - time.monotonic()
            if verification.should_recover(
                audit_initial, budget_remaining_s=budget_remaining, attempts=recovery_attempts,
                max_recoveries=config.VR_MAX_RECOVERIES, min_confidence=config.VR_MIN_CONFIDENCE,
            ):
                recovery_triggered = True
                _timeline_event(timeline, "REPLAN")
                recovery_dir = vr_dir / "recovery_1"
                recovery_dir.mkdir(parents=True, exist_ok=True)
                try:
                    screenshot_ref = audit_dir / "screenshots" / "pre_recovery.png"
                    screenshot_ref.parent.mkdir(parents=True, exist_ok=True)
                    screenshot_ref.write_bytes(ctrl.screenshot())
                    a11y_text = ctrl.a11y_tree()
                except Exception as e:
                    screenshot_ref, a11y_text = "<capture failed>", f"<capture failed: {e}>"
                contract = verification.build_recovery_contract(
                    task, claim_initial, audit_initial, screenshot_ref=screenshot_ref,
                    a11y_text=a11y_text, budget_remaining_s=budget_remaining,
                )
                (recovery_dir / "contract.json").write_text(json.dumps(contract, indent=2))
                recovery_prompt = (
                    "You are picking up a task from another session that believed it was "
                    "finished, but an independent review disagreed. Do not start over -- fix "
                    "only what is broken.\n\n" + json.dumps(contract, indent=2)
                    + "\n\n" + verification.EXECUTOR_CLAIM_CONTRACT_SUFFIX
                )
                _timeline_event(timeline, "EXECUTE_RECOVERY")
                cmd_recovery = build_claude_cmd(
                    recovery_prompt, model=config.MODEL or None,
                    max_turns=config.VR_RECOVERY_MAX_TURNS, mcp_config=full_mcp_path,
                    allowed_tools=OSWORLD_TOOLS,
                )
                meta_recovery = run_claude_meta(cmd_recovery, timeout=config.VR_RECOVERY_TIMEOUT)
                recovery_attempts = 1
                if meta_recovery.get("api_error_status"):
                    # Section 11: RECOVERY_INFRA_ERROR -- score whatever state exists, annotated.
                    _timeline_event(timeline, "RECOVERY_INFRA_ERROR")
                else:
                    text_recovery = meta_recovery.get("result", "")
                    answer_recovery = extract_answer(text_recovery)
                    claim_recovery = verification.parse_executor_claim(text_recovery)
                    (recovery_dir / "output.txt").write_text(text_recovery)
                    (recovery_dir / "claim.json").write_text(json.dumps(claim_recovery, indent=2))
                    _save_role_transcript(meta_recovery, recovery_dir, task["id"], "recovery_1")
                    role_usage["recovery_1"] = _role_result(meta_recovery, "recovery_1")
                    if answer_recovery:
                        final_answer, final_meta = answer_recovery, meta_recovery
                    _timeline_event(timeline, "EXECUTE_RECOVERY_DONE",
                                    claim_status=claim_recovery["status"])

                    final_audit_dir = vr_dir / "audit_final"
                    final_audit_dir.mkdir(parents=True, exist_ok=True)
                    _timeline_event(timeline, "FINAL_AUDIT")
                    cmd_final_audit = build_claude_cmd(
                        verification.render_auditor_prompt(task, claim_recovery),
                        model=config.VR_AUDITOR_MODEL or config.MODEL or None,
                        max_turns=config.VR_FINAL_AUDITOR_MAX_TURNS,
                        mcp_config=_readonly_mcp_path, allowed_tools=READONLY_TOOLS,
                    )
                    meta_final_audit = run_claude_meta(cmd_final_audit, timeout=config.VR_AUDIT_TIMEOUT)
                    if meta_final_audit.get("api_error_status"):
                        audit_final = verification.audit_error("final auditor rate-limited")
                    else:
                        audit_final = verification.parse_audit_report(
                            meta_final_audit.get("result", ""))
                        (final_audit_dir / "report.json").write_text(
                            json.dumps(audit_final, indent=2))
                        _save_role_transcript(meta_final_audit, final_audit_dir, task["id"],
                                              "audit_final")
                        role_usage["audit_final"] = _role_result(meta_final_audit, "audit_final")
                    _timeline_event(timeline, "FINAL_AUDIT_DONE", verdict=audit_final["verdict"])
    finally:
        for p in (full_mcp_path, _readonly_mcp_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # === SCORE (official evaluator invoked exactly once, VM still live) ===
    clean_finish = _clean_finish(final_meta, final_answer)
    eval_state = _bounded("eval-state capture", _capture_eval_state, ctrl, task, out) if ctrl else None
    telemetry = _agent_telemetry(final_meta)
    telemetry["agent_clean_finish"] = clean_finish

    last_audit = audit_final or audit_initial
    false_completion_detected = bool(
        claim_initial["status"] == "done" and audit_initial and audit_initial["verdict"] == "not_done")
    false_completion_recovered = bool(
        recovery_triggered and audit_final and audit_final["verdict"] == "verified_done")

    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "instruction": task["instruction"],
        "answer": final_answer,
        "eval_state": eval_state,
        "provenance": _provenance(task, ctrl, started_at),
        "harness": "verify_replan",
        "harness_schema": 1,
        "initial_claim": claim_initial["status"],
        "audit_initial": audit_initial["verdict"] if audit_initial else None,
        "recovery_triggered": recovery_triggered,
        "recovery_attempts": recovery_attempts,
        "audit_final": last_audit["verdict"] if last_audit else None,
        # Best-effort proxies, not a counterfactual (that would need a second, no-recovery run
        # of the SAME trajectory, which this harness deliberately never does -- see Section 3.4
        # on natural vs matched budget reporting for the honest way to establish causal effect):
        # "detected" = the initial audit disagreed with a self-reported DONE; "recovered" = a
        # recovery was attempted AND the final audit came back verified_done afterward.
        "false_completion_detected": false_completion_detected,
        "false_completion_recovered": false_completion_recovered,
        "role_usage": role_usage,
        "total_agent_cost_usd": _sum(role_usage, "agent_cost_usd"),
        "total_agent_duration_ms": _sum(role_usage, "agent_duration_ms"),
        **_model_mismatch(final_meta),
        **telemetry,
    })
    (vr_dir / "manifest.json").write_text(json.dumps({
        "harness_schema": 1,
        "config": {
            "vr_max_recoveries": config.VR_MAX_RECOVERIES,
            "vr_min_confidence": config.VR_MIN_CONFIDENCE,
            "vr_initial_max_turns": config.VR_INITIAL_MAX_TURNS,
            "vr_auditor_max_turns": config.VR_AUDITOR_MAX_TURNS,
            "vr_recovery_max_turns": config.VR_RECOVERY_MAX_TURNS,
            "vr_final_auditor_max_turns": config.VR_FINAL_AUDITOR_MAX_TURNS,
            "auditor_model": config.VR_AUDITOR_MODEL or config.MODEL or None,
        },
        "prompt_versions": verification.PROMPT_VERSIONS,
    }, indent=2))
    (vr_dir / "timeline.jsonl").write_text(
        "\n".join(json.dumps(e) for e in timeline) + "\n")

    rec = _annotate_incidental(
        _bounded("scoring", _score, ctrl, task, final_answer, out), clean_finish)
    results_io.write_eval(out, {"id": task["id"], **rec})
    return final_answer
