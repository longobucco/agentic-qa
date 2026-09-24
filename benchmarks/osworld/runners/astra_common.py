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
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config
from benchmarks.osworld.runners.agent_computer import _provenance
from benchmarks.osworld.runners.common import _host_path_markers
from core.codex_loop import session_context

_SUSPICIOUS = ("evaluator", "gold", "results", "task_spec")


def api_error_status(meta, stderr=""):
    """Codex does not always put a usage limit in the JSONL error stream -- it can land only on
    stderr. Missing it would let a throttled run be scored as if the agent had had its chance.

    Also detects OAuth token revocation ("refresh_token_invalidated" / 401 Unauthorized),
    confirmed live 2026-09-15: a sustained rate-limit window was followed by the Codex CLI's own
    session being revoked server-side ("Your access token could not be refreshed because your
    refresh token was revoked"), which produces agent_num_turns=0 / stop_reason=error but was
    NOT caught by the rate-limit patterns below -- 4 runs across 2 tasks were scored as genuine
    FAILURE (reward 0.0, an evaluator run against an empty answer) before this was caught, a
    real, permanent, unretried contamination since FAILURE is not in core.reporting's
    _INCONCLUSIVE_VERDICTS. Returns the string "AUTH_REVOKED" (distinct from the int 429) so
    callers can tell the two apart -- unlike a rate limit, backing off and retrying does nothing
    for this until a human re-authenticates the Codex CLI."""
    text = (json.dumps(meta.get("errors") or "") + " " + (stderr or "")).lower()
    if ("refresh_token_invalidated" in text or "refresh token was revoked" in text
            or "token_revoked" in text or "401 unauthorized" in text):
        return "AUTH_REVOKED"
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


# Content kinds Codex's own harness injects into every session regardless of host, which no
# supported 0.153.4 knob removes: the multi-agent role/mode text comes from the model catalog's
# model_messages for gpt-6-astra (multi_agent_version v2), not from this machine. Plus the task.
CODEX_HARNESS_KINDS = frozenset({
    "user.text", "multi_agent.role_instructions", "multi_agent.mode_instructions",
})


def codex_rollout_context_leaks(path):
    """Host/harness context Codex put in front of the model in a session (sorted, [] when clean),
    read from its rollout (the `exec --json` stream never shows it). Every input message carries
    Codex's own content_item_kinds label (environments.environment_context, host_skills.
    instructions, permissions.instructions, AGENTS.md project docs, hook context, ...); any kind
    outside CODEX_HARNESS_KINDS is reported by that label -- unknown kinds count, so a new CLI
    can't add context silently. Also `base_instructions:<provenance>` unless the base
    instructions are ours (provenance "custom"), and repo_path when the checkout or the
    harness cwd appears anywhere in the session. Upstream's agent sees only its system prompt
    and the task."""
    raw = Path(path).read_text(errors="replace")
    leaks = set()
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        if not isinstance(payload, dict):
            continue
        if rec.get("type") == "session_meta":
            provenance = ((payload.get("base_instructions") or {}).get("provenance") or {})
            if provenance.get("type") != "custom":
                leaks.add(f"base_instructions:{provenance.get('type')}")
        elif (rec.get("type") == "response_item" and payload.get("type") == "message"
              and payload.get("role") in ("developer", "user", "system")):
            meta = payload.get("internal_chat_message_metadata_passthrough") or {}
            leaks.update(k for k in meta.get("content_item_kinds") or ["unlabelled"]
                         if k not in CODEX_HARNESS_KINDS)
    if any(m in raw for m in _host_path_markers()):
        leaks.add("repo_path")
    return sorted(leaks)


# How the model reaches the desktop through Codex's Code Mode host: `exec` runs its JavaScript,
# which calls the deferred MCP tool as tools.mcp__osworld__computer(...); `wait` resumes a
# yielded exec cell. Anything else the model calls is a tool other than `computer`.
CODEX_OFFICIAL_NESTED_TOOL = "mcp__osworld__computer"
CODEX_EXEC_PLUMBING = frozenset({"exec", "wait"})
_EXEC_TOOL_REF_RE = re.compile(
    r"(?<![\w$.])tools\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*(?:(['\"`])([^'\"`]*)\2\s*\]|[^\]]*\]))")


def codex_rollout_tool_calls(path):
    """Tools other than `computer` the model called in a session, in order, from Codex's rollout.
    Needed because Code Mode calls never reach the `exec --json` stream (verified live on
    0.153.4: an exec call emits no item at all; only the MCP calls it makes do): every function
    call other than CODEX_EXEC_PLUMBING, and every `tools.<name>` an exec script references other
    than the computer tool, as `exec.<name>` -- `exec.<dynamic>` when the name is computed, so it
    can't hide behind an expression."""
    names = []
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        if not isinstance(payload, dict) or rec.get("type") != "response_item":
            continue
        kind, name = payload.get("type"), payload.get("name")
        if kind not in ("function_call", "custom_tool_call"):
            continue
        if name == "exec" and kind == "custom_tool_call":
            for dotted, _, quoted in _EXEC_TOOL_REF_RE.findall(str(payload.get("input") or "")):
                ref = dotted or quoted or "<dynamic>"
                if ref != CODEX_OFFICIAL_NESTED_TOOL:
                    names.append(f"exec.{ref}")
        elif name not in CODEX_EXEC_PLUMBING:
            names.append(name)
    return names


def rate_limit_rec(task, status):
    """`status` is either the int 429 (a real, self-clearing rate limit -- the driver's backoff
    is the right response) or the string "AUTH_REVOKED" (the Codex CLI session itself is dead --
    no amount of waiting fixes this, a human has to re-authenticate). Distinct outcomes so a
    campaign driver can tell them apart and stop instead of backing off pointlessly."""
    outcome = "AUTH_ERROR" if status == "AUTH_REVOKED" else "RATE_LIMITED"
    return {
        "id": task["id"], "outcome": outcome, "error_type": "APIError",
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
