"""Codex/GPT-Astra-specific shared primitives, extracted from runners/gpt_astra.py so a second
runner (runners/gpt_astra_openbook.py) can reuse the exact same telemetry, rate-limit detection,
tool audit and Codex-provenance logic without duplicating it.

Deliberately separate from runners/common.py: that module is Claude-CLI-specific (its own
docstring says so -- MCP config shape for the `claude` binary, `~/.claude/projects` transcript
lookup, `modelUsage` parsing), none of which applies to Codex's JSONL stream or
`~/.codex/sessions` rollout files. Nothing here reads a closed-book-only policy knob (there isn't
one yet) -- both runners import the same names, no runner-specific branching lives in this file.
"""
import json
import subprocess
from datetime import datetime, timezone

from benchmarks.osworld import config
from benchmarks.osworld.runners.agent_computer import _provenance
from core.codex_loop import session_context

_SUSPICIOUS = ("evaluator", "gold", "results", "task_spec")


def api_error_status(meta, stderr=""):
    """Codex does not always put a usage limit in the JSONL error stream -- it can land only on
    stderr. Missing it would let a throttled run be scored as if the agent had had its chance."""
    text = (json.dumps(meta.get("errors") or "") + " " + (stderr or "")).lower()
    if "429" in text or "rate limit" in text or "usage limit" in text or "quota" in text:
        return 429
    return None


def estimated_api_cost(input_tokens, cached_tokens, output_tokens):
    if input_tokens is None or output_tokens is None:
        return None
    cached = cached_tokens or 0
    uncached = max(0, input_tokens - cached)
    return round((uncached * 10 + cached * 1 + output_tokens * 50) / 1_000_000, 6)


def telemetry(meta, stderr=""):
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
        "agent_api_error_status": api_error_status(meta, stderr),
        # Codex JSONL does not report the user's actual charge (ChatGPT pooled usage versus API
        # billing differ), so never label a token-derived estimate as billed cost.
        "agent_cost_usd": usage.get("cost_usd") or usage.get("total_cost_usd"),
        "agent_estimated_api_cost_usd": estimated_api_cost(
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


def codex_version():
    try:
        return subprocess.run(["codex", "--version"], check=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return None


def check_codex_cli(*, model, reasoning_effort, codex_cli_version):
    """codex binary present/logged-in, its --version matches the pin, and the requested
    model+reasoning_effort are in its own model catalog. Shared by both Astra runners' preflight
    -- extracted so the open-book preflight doesn't re-implement (and drift from) this check
    against a different lock file's model/effort/version fields."""
    import shutil

    binary = shutil.which("codex")
    if not binary:
        raise SystemExit("codex CLI not found on PATH; install/login before running GPT Astra")
    try:
        version_text = subprocess.run(
            [binary, "--version"], check=True, capture_output=True, text=True, timeout=15
        ).stdout.strip()
        actual_version = version_text.rsplit(" ", 1)[-1]
        if codex_cli_version and actual_version != codex_cli_version:
            raise SystemExit(
                f"codex CLI version mismatch: campaign pins {codex_cli_version}, "
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
        model_entry = next((m for m in catalog.get("models", []) if m.get("slug") == model), None)
        if not model_entry:
            raise SystemExit(f"{model!r} is absent from the Codex model catalog")
        supported = {x.get("effort") for x in model_entry.get("supported_reasoning_levels", [])}
        if reasoning_effort not in supported:
            raise SystemExit(
                f"reasoning effort {reasoning_effort!r} is unsupported for {model}; "
                f"supported={sorted(supported)}"
            )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise SystemExit(f"codex CLI preflight failed: {exc}") from exc


def provenance_astra(task, ctrl, started_at, codex_ver, *, model, reasoning_effort,
                     session_id=None, extra=None):
    """Per-run Codex provenance. `model`/`reasoning_effort` are passed explicitly (not read off
    config.ASTRA_*) so the open-book runner, which may run under its own campaign lock, is not
    silently coupled to the closed-book runner's env vars."""
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
        "model_requested": model,
        "model_served": served_model,
        "model_pinned": True,
        "model_mismatch": (served_model != model) if served_model else None,
        "reasoning_effort_mismatch": (
            served_effort != reasoning_effort) if served_effort else None,
        **{k: v for k, v in served.items() if k != "model_served"},
        "agent_runtime": "codex_cli",
        "agent_runtime_version": codex_ver,
        "reasoning_effort": reasoning_effort or None,
        # Codex has no Claude-style --max-turns flag; the shared wall-clock and the prompt's
        # action budget are the applicable controls for this runner.
        "max_turns": None,
        "max_steps": config.MAX_STEPS,
        **(extra or {}),
    })
    return rec


def rate_limit_rec(task, status):
    return {
        "id": task["id"], "outcome": "RATE_LIMITED", "error_type": "APIError",
        "error": f"api_error_status={status}",
        "at": datetime.now(timezone.utc).isoformat(),
    }


def write_tool_audit(out, events):
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
