"""Pure logic for the Verify-Replan harness (docs/verify-replan-minimal-integration-plan.md
Sections 5, 12): structured-claim/audit-report parsing, prompt generation, and the recovery
decision. No Daytona/controller import, no MCP, no `claude -p` invocation -- everything here is
a plain function of its inputs, so runners/verify_replan.py's orchestration can be exercised with
a fake CLI (Section 12's integration test tier) while this module gets ordinary unit tests.

Contract versioning: EXECUTOR_CLAIM_SCHEMA/AUDIT_REPORT_SCHEMA are version tags, not JSON Schema
documents -- Section 9's "ogni template deve avere versione e SHA-256 registrati" is satisfied by
PROMPT_VERSIONS below (name -> (version, sha256 of the template text)), computed once at import
time so a prompt edit is visible in every manifest.json without hand-maintaining a hash.
"""
import hashlib
import json

EXECUTOR_CLAIM_SCHEMA = 1
AUDIT_REPORT_SCHEMA = 1

_VALID_EXECUTOR_STATUS = {"done", "infeasible", "blocked"}
_VALID_AUDIT_VERDICT = {"verified_done", "not_done", "infeasible", "uncertain", "audit_error"}
_VALID_CONFIDENCE = {"low": 0, "medium": 1, "high": 2}


def _top_level_object_spans(text):
    """Finds every complete TOP-LEVEL {...} span (brace depth 0 -> 1 -> ... -> 0), aware of JSON
    string literals so a brace character inside a string value (e.g. claim text describing UI
    braces, or an escaped quote) never perturbs the depth count. A naive scan for the last '{'
    character in the text would instead find the innermost/rightmost NESTED object -- e.g. the
    single object in `{"observations": [{"fact": "x"}]}` has its last '{' belonging to the inner
    observation dict, not the outer report -- which silently returns a fragment missing every
    top-level field."""
    spans = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, c in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append(text[start:i + 1])
                    start = None
    return spans


def _last_json_object(text):
    """Tolerant extraction: the LAST top-level {...} span in the text that parses as a JSON
    object, so a claim or audit block embedded in narrative text (before or after it, or
    preceded by an earlier/irrelevant object) is found regardless of what surrounds it. None if
    nothing parses -- a missing or malformed block, which callers must treat as "no structured
    data", not a crash (Section 5.1: "Un blocco mancante non annulla la traiettoria")."""
    if not text:
        return None
    for span in reversed(_top_level_object_spans(text)):
        try:
            obj = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_executor_claim(text):
    """Returns a claim dict, always with a `status` key -- "blocked" with a parse_error note if
    no valid block was found or required fields are missing/malformed, never an exception. The
    Auditor is designed to receive this as-is (Section 5.1: original task + whatever claim text
    is available), so a parse failure here degrades the audit's input, it doesn't skip the audit."""
    obj = _last_json_object(text)
    if obj is None or "status" not in obj:
        return {"status": "blocked", "claim": (text or "").strip()[:2000],
                "evidence_to_check": [], "remaining_risk": None,
                "parse_error": "no valid claim JSON block found in executor output"}
    status = obj.get("status")
    if status not in _VALID_EXECUTOR_STATUS:
        return {"status": "blocked", "claim": obj.get("claim") or (text or "").strip()[:2000],
                "evidence_to_check": obj.get("evidence_to_check") or [],
                "remaining_risk": obj.get("remaining_risk"),
                "parse_error": f"invalid status {status!r}, expected one of {sorted(_VALID_EXECUTOR_STATUS)}"}
    evidence = obj.get("evidence_to_check")
    return {
        "status": status,
        "claim": obj.get("claim") or "",
        "evidence_to_check": evidence if isinstance(evidence, list) else [],
        "remaining_risk": obj.get("remaining_risk"),
        "parse_error": None,
    }


def parse_audit_report(text):
    """Same tolerance discipline as parse_executor_claim: a malformed or missing audit block
    becomes verdict="audit_error" (Section 11's AUDIT_HARNESS_ERROR path), never an exception
    that would crash the run."""
    obj = _last_json_object(text)
    if obj is None:
        return _audit_error("no valid audit JSON block found in auditor output")
    verdict = obj.get("verdict")
    if verdict not in _VALID_AUDIT_VERDICT:
        return _audit_error(f"invalid verdict {verdict!r}, expected one of "
                            f"{sorted(_VALID_AUDIT_VERDICT)}")
    confidence = obj.get("confidence")
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"   # missing/invalid confidence never defaults to something recoverable
    observations = obj.get("observations")
    failed_checks = obj.get("failed_checks")
    return {
        "schema_version": AUDIT_REPORT_SCHEMA,
        "verdict": verdict,
        "observations": observations if isinstance(observations, list) else [],
        "failed_checks": failed_checks if isinstance(failed_checks, list) else [],
        "next_action": obj.get("next_action"),
        "confidence": confidence,
        "parse_error": None,
    }


def audit_error(reason):
    """Public constructor for the audit_error shape -- used both internally (a malformed/missing
    audit block) and by callers that hit an infra failure before the auditor ever produced text
    (Section 11: AUDIT_INFRA_ERROR), so both paths score identically downstream."""
    return {
        "schema_version": AUDIT_REPORT_SCHEMA, "verdict": "audit_error", "observations": [],
        "failed_checks": [], "next_action": None, "confidence": "low", "parse_error": reason,
    }


_audit_error = audit_error   # internal alias, kept short at call sites within this module


def should_recover(audit, *, budget_remaining_s, attempts, max_recoveries, min_confidence="medium"):
    """Pure decision: recovery is authorized only when ALL of these hold (Sections 5.2, 8):
    - verdict is exactly "not_done" (not infeasible/uncertain/verified_done/audit_error --
      Section 5.2: "uncertain non viene automaticamente trasformato in failure");
    - at least one observation AND a concrete next_action (a bare "not_done" with nothing to
      act on is not "evidence", it's an unsubstantiated audit -- never authorizes a recovery
      attempt against it);
    - confidence meets the configured floor (OSW_VR_MIN_CONFIDENCE, Section 8: "non recuperare
      su low-confidence" by default);
    - the recovery attempt budget and the wall-clock budget both still have room.

    Never negative budget in, never negative budget implied out -- callers pass whatever is
    left, this function only reads it.
    """
    if audit.get("verdict") != "not_done":
        return False
    if not audit.get("observations") or not audit.get("next_action"):
        return False
    if _VALID_CONFIDENCE.get(audit.get("confidence"), -1) < _VALID_CONFIDENCE.get(min_confidence, 1):
        return False
    if attempts >= max_recoveries:
        return False
    if budget_remaining_s <= 0:
        return False
    return True


def build_recovery_contract(task, executor_claim, audit_report, *, screenshot_ref, a11y_text,
                            budget_remaining_s):
    """Section 5.3: exactly what the Recovery Executor receives -- never the evaluator spec,
    gold, or reward (that boundary lives in runners/verify_replan.py, which is the only place
    that ever sees those fields at all; this function's signature has no parameter that could
    carry them, which is itself part of the boundary)."""
    return {
        "task_instruction": task.get("instruction", ""),
        "prior_claim": executor_claim,
        "audit_report": audit_report,
        "latest_screenshot_ref": str(screenshot_ref),
        "latest_a11y_tree": a11y_text,
        "budget_remaining_s": max(0, budget_remaining_s),
        "instruction_to_executor": (
            "Correct ONLY the failed checks listed in audit_report.failed_checks, using "
            "audit_report.next_action as guidance. Do not redo work that is not implicated. "
            "Re-observe the current state before acting. Then verify your own fix before "
            "answering again."
        ),
    }


EXECUTOR_CLAIM_CONTRACT_SUFFIX = """
When you finish (or determine the task cannot be completed as stated), your final message must
end with your usual ANSWER: line, followed by a JSON block on its own with exactly this shape:

{
  "status": "done" | "infeasible" | "blocked",
  "claim": "one or two sentences describing the state you believe you achieved",
  "evidence_to_check": ["a specific, observable thing that would confirm this", "..."],
  "remaining_risk": "anything you are not fully sure about, or null"
}

A separate, independent process will verify your claim against the actual desktop state -- it
cannot see this conversation, only the current screen and this claim. Be concrete and specific
in evidence_to_check: name exact UI elements, file names, or values, not vague descriptions."""


AUDITOR_SYSTEM_PROMPT = """You are an independent auditor. You did NOT perform this task and \
have NO memory of how it was attempted. You can only observe the current desktop state through \
the tools available to you (screenshot, accessibility tree) -- you cannot click, type, or take \
any action.

Task the executor was asked to do: {instruction}

The executor's own claim (do not simply trust this -- verify it against what you can actually \
observe):
{claim_json}

Instructions:
- Check the goal using ONLY the state you can observe yourself.
- Do not assume steps were taken that aren't visible.
- Do not attempt any corrective action -- you cannot mutate the desktop, and you must not try.
- Do not deduce success from an application merely being open, or a file merely existing,
  without checking its actually relevant content.
- If you cannot determine the state with confidence, say so honestly (verdict "uncertain")
  rather than guessing either way.

Respond with exactly one JSON block, on its own, with this shape:

{{
  "schema_version": 1,
  "verdict": "verified_done" | "not_done" | "infeasible" | "uncertain",
  "observations": [
    {{"fact": "specific fact you observed", "source": "screenshot" | "a11y", "evidence_ref": null}}
  ],
  "failed_checks": ["specific condition from the task that is not satisfied, if any"],
  "next_action": "a concrete correction, without prescribing exact pixel coordinates",
  "confidence": "high" | "medium" | "low"
}}"""


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


# Section 9: "Ogni template deve avere versione e SHA-256 registrati." -- computed once here so
# a manifest can cite {"name": version, "sha256": ...} without hand-maintaining a hash that
# silently goes stale the next time a prompt's wording changes.
PROMPT_VERSIONS = {
    "executor_claim_contract_suffix": {"version": 1, "sha256": _hash(EXECUTOR_CLAIM_CONTRACT_SUFFIX)},
    "auditor_system_prompt": {"version": 1, "sha256": _hash(AUDITOR_SYSTEM_PROMPT)},
}


def render_auditor_prompt(task, executor_claim):
    return AUDITOR_SYSTEM_PROMPT.format(
        instruction=task.get("instruction", ""),
        claim_json=json.dumps(executor_claim, indent=2),
    )
