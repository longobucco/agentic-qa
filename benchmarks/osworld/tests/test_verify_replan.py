"""Unit tests for benchmarks/osworld/verification.py -- the pure claim/audit parsing and
recovery-decision logic behind the Verify-Replan harness
(docs/verify-replan-minimal-integration-plan.md Section 12: "parsing tollerante dei blocchi
JSON... tutte le transizioni della state machine... cap di recovery... budget residuo mai
negativo"). No Daytona, no MCP, no claude -p:
  python -m benchmarks.osworld.tests.test_verify_replan
"""
import json

from benchmarks.osworld import verification as vr


# ---------------------------------------------------------------------------
# parse_executor_claim
# ---------------------------------------------------------------------------

def test_parse_executor_claim_happy_path():
    text = 'ANSWER: DONE\n' + json.dumps({
        "status": "done", "claim": "renamed the file",
        "evidence_to_check": ["file explorer shows new_name.txt"], "remaining_risk": None,
    })
    claim = vr.parse_executor_claim(text)
    assert claim["status"] == "done"
    assert claim["claim"] == "renamed the file"
    assert claim["evidence_to_check"] == ["file explorer shows new_name.txt"]
    assert claim["parse_error"] is None


def test_parse_executor_claim_finds_the_block_even_with_narrative_text_around_it():
    text = ("I looked at the task and did the following steps...\n"
            + json.dumps({"status": "infeasible", "claim": "no such menu exists"})
            + "\nThat's my final answer.")
    claim = vr.parse_executor_claim(text)
    assert claim["status"] == "infeasible" and claim["parse_error"] is None


def test_parse_executor_claim_picks_the_last_json_object_when_several_are_present():
    """An executor might echo the task's own JSON (e.g. an evaluator config it printed while
    reasoning) before its real final claim -- the LAST valid object in the text is the one that
    matters, matching how ANSWER: itself is always the last line, not the first."""
    text = (json.dumps({"status": "blocked", "claim": "an earlier, irrelevant object"})
            + "\n...\n"
            + json.dumps({"status": "done", "claim": "the real final claim"}))
    claim = vr.parse_executor_claim(text)
    assert claim["claim"] == "the real final claim"


def test_parse_executor_claim_missing_block_is_blocked_not_a_crash():
    """Section 5.1: 'Un blocco mancante non annulla la traiettoria' -- a missing contract
    degrades gracefully into a status the Auditor can still receive, it never raises."""
    claim = vr.parse_executor_claim("ANSWER: DONE\nI finished the task.")
    assert claim["status"] == "blocked"
    assert claim["parse_error"] is not None
    assert "I finished the task" in claim["claim"]


def test_parse_executor_claim_empty_text_is_blocked_not_a_crash():
    assert vr.parse_executor_claim("")["status"] == "blocked"
    assert vr.parse_executor_claim(None)["status"] == "blocked"


def test_parse_executor_claim_rejects_an_invalid_status_value():
    text = json.dumps({"status": "success", "claim": "x"})   # not a valid enum value
    claim = vr.parse_executor_claim(text)
    assert claim["status"] == "blocked"
    assert "invalid status" in claim["parse_error"]


def test_parse_executor_claim_coerces_a_non_list_evidence_field():
    text = json.dumps({"status": "done", "claim": "x", "evidence_to_check": "not a list"})
    claim = vr.parse_executor_claim(text)
    assert claim["evidence_to_check"] == []


# ---------------------------------------------------------------------------
# parse_audit_report
# ---------------------------------------------------------------------------

def test_parse_audit_report_happy_path():
    text = json.dumps({
        "schema_version": 1, "verdict": "not_done",
        "observations": [{"fact": "no dialog visible", "source": "screenshot"}],
        "failed_checks": ["dialog was never dismissed"],
        "next_action": "dismiss the remaining dialog", "confidence": "high",
    })
    audit = vr.parse_audit_report(text)
    assert audit["verdict"] == "not_done"
    assert audit["confidence"] == "high"
    assert audit["parse_error"] is None


def test_parse_audit_report_missing_block_becomes_audit_error():
    """Section 11: a parser failure after a single retry maps to AUDIT_HARNESS_ERROR, scored
    without recovery -- never an exception that would strand the whole task."""
    audit = vr.parse_audit_report("I could not form a clean verdict here.")
    assert audit["verdict"] == "audit_error"
    assert audit["parse_error"] is not None


def test_parse_audit_report_rejects_an_invalid_verdict():
    text = json.dumps({"verdict": "looks_fine", "confidence": "high"})
    audit = vr.parse_audit_report(text)
    assert audit["verdict"] == "audit_error"


def test_parse_audit_report_defaults_a_missing_confidence_to_low_not_medium():
    """A missing confidence must never silently default to something that clears the
    should_recover floor (medium) -- that would let a malformed-but-plausible-looking audit
    authorize a recovery attempt it never earned."""
    text = json.dumps({"verdict": "not_done", "observations": [{"fact": "x"}],
                       "next_action": "y"})
    audit = vr.parse_audit_report(text)
    assert audit["confidence"] == "low"


def test_parse_audit_report_coerces_non_list_observations_and_failed_checks():
    text = json.dumps({"verdict": "uncertain", "observations": "not a list",
                       "failed_checks": None, "confidence": "medium"})
    audit = vr.parse_audit_report(text)
    assert audit["observations"] == [] and audit["failed_checks"] == []


# ---------------------------------------------------------------------------
# should_recover
# ---------------------------------------------------------------------------

_DEFAULT_OBSERVATIONS = [{"fact": "x"}]


def _audit(verdict="not_done", observations=_DEFAULT_OBSERVATIONS, next_action="fix it",
          confidence="high"):
    # NOT `observations or _DEFAULT_OBSERVATIONS`: an explicitly empty list must stay empty
    # for the "false without observations" test below, not fall back to the default.
    return {"verdict": verdict, "observations": observations,
            "next_action": next_action, "confidence": confidence}


def test_should_recover_true_on_a_well_formed_not_done_audit_with_room_left():
    assert vr.should_recover(_audit(), budget_remaining_s=100, attempts=0, max_recoveries=1)


def test_should_recover_false_for_every_non_not_done_verdict():
    for verdict in ("verified_done", "infeasible", "uncertain", "audit_error"):
        assert not vr.should_recover(_audit(verdict=verdict), budget_remaining_s=100,
                                     attempts=0, max_recoveries=1)


def test_should_recover_false_without_observations_or_next_action():
    assert not vr.should_recover(_audit(observations=[]), budget_remaining_s=100,
                                 attempts=0, max_recoveries=1)
    assert not vr.should_recover(_audit(next_action=None), budget_remaining_s=100,
                                 attempts=0, max_recoveries=1)


def test_should_recover_false_below_the_confidence_floor():
    assert not vr.should_recover(_audit(confidence="low"), budget_remaining_s=100,
                                 attempts=0, max_recoveries=1, min_confidence="medium")
    assert vr.should_recover(_audit(confidence="medium"), budget_remaining_s=100,
                             attempts=0, max_recoveries=1, min_confidence="medium")


def test_should_recover_false_once_the_recovery_cap_is_reached():
    assert not vr.should_recover(_audit(), budget_remaining_s=100, attempts=1, max_recoveries=1)
    assert not vr.should_recover(_audit(), budget_remaining_s=100, attempts=5, max_recoveries=1)


def test_should_recover_false_with_no_budget_left():
    assert not vr.should_recover(_audit(), budget_remaining_s=0, attempts=0, max_recoveries=1)
    assert not vr.should_recover(_audit(), budget_remaining_s=-5, attempts=0, max_recoveries=1)


# ---------------------------------------------------------------------------
# build_recovery_contract
# ---------------------------------------------------------------------------

def test_recovery_contract_never_leaks_evaluator_or_gold_fields():
    """Section 5.3: 'Non riceve evaluator spec, gold o reward.' The function's own signature
    has no parameter for those, so this test is really just confirming the contract's rendered
    shape has no field that could carry them even if a caller tried to smuggle them through
    kwargs -- there are none to smuggle through."""
    contract = vr.build_recovery_contract(
        {"instruction": "do X", "evaluator": {"func": "secret_check"}},
        {"status": "done", "claim": "x"}, vr.parse_audit_report(json.dumps({
            "verdict": "not_done", "observations": [{"fact": "x"}], "next_action": "y",
            "confidence": "high"})),
        screenshot_ref="/tmp/a.png", a11y_text="<tree/>", budget_remaining_s=500)
    assert "evaluator" not in json.dumps(contract)
    assert "secret_check" not in json.dumps(contract)
    assert contract["task_instruction"] == "do X"


def test_recovery_contract_clamps_budget_to_never_go_negative():
    contract = vr.build_recovery_contract(
        {"instruction": "x"}, {"status": "done"}, vr.parse_audit_report("{}"),
        screenshot_ref="/tmp/a.png", a11y_text="", budget_remaining_s=-50)
    assert contract["budget_remaining_s"] == 0


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def test_render_auditor_prompt_embeds_the_instruction_and_claim():
    prompt = vr.render_auditor_prompt({"instruction": "rename the file"},
                                      {"status": "done", "claim": "renamed it"})
    assert "rename the file" in prompt
    assert "renamed it" in prompt


def test_prompt_versions_are_registered_with_a_stable_hash():
    assert "auditor_system_prompt" in vr.PROMPT_VERSIONS
    entry = vr.PROMPT_VERSIONS["auditor_system_prompt"]
    assert entry["version"] == 1 and len(entry["sha256"]) == 64


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
