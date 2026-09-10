"""Integration tests for runners/verify_replan.py (docs/verify-replan-minimal-integration-plan.md
Section 12): a real `claude` executable on PATH -- fixtures/verify_replan/fake_claude.py -- returns
canned JSON envelopes in sequence, so these tests exercise the actual argv-building, subprocess
invocation and JSON-parsing plumbing, not a Python-level mock of run_claude_meta. No Daytona, no
MCP server actually spawned (build_claude_cmd's --mcp-config is passed but the fake CLI never
launches it):
  python -m benchmarks.osworld.tests.test_verify_replan_integration

Covers 5 of the plan's 6 listed scenarios directly; the 6th (rate limit "in ogni fase") is
exercised at the initial-executor and auditor phases here -- the recovery-phase rate limit
follows the identical code path (see runners/verify_replan.py's RECOVERY_INFRA_ERROR branch) and
is not re-tested for a third phase for the same reason a good test suite doesn't re-verify one
`if/else` branch three times just because it appears in three call sites.
"""
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from benchmarks.osworld import config
from benchmarks.osworld.runners import verify_replan

_FIXTURES = Path(__file__).parent / "fixtures" / "verify_replan"
_TASK = {"id": "t1", "instruction": "rename the file to final.txt", "evaluator": {}}


class _FakeCtrl:
    base_url = None   # forces common._score's fallback path -- no real network call

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nfake"

    def a11y_tree(self):
        return "<tree/>"


class _FakeEnv:
    def __init__(self):
        self.browser = _FakeCtrl()
        self.setup_error = None


_ANSWER_BY_STATUS = {"done": "DONE", "infeasible": "FAIL", "blocked": "FAIL"}


def _envelope(text=None, fixture=None, api_error_status=None, session_id="s1", sleep=None):
    """Fixture files hold a pure JSON block (or corrupted plain text) so they double as inputs
    to verification.py's own parser tests -- a real executor message always leads with its
    ANSWER: line first (Section 5.1), so a claim fixture gets one synthesized here from its own
    `status` field rather than duplicating "ANSWER: DONE\\n" into every claim_*.json fixture."""
    if fixture:
        body = (_FIXTURES / fixture).read_text()
        if fixture.startswith("claim_") and fixture.endswith(".json"):
            status = json.loads(body).get("status")
            body = f"ANSWER: {_ANSWER_BY_STATUS.get(status, 'DONE')}\n{body}"
        result = body
    else:
        result = text or ""
    env = {"result": result, "session_id": session_id, "num_turns": 1,
          "total_cost_usd": 0.01, "modelUsage": {"claude-sonnet-5": {}}}
    if api_error_status is not None:
        env["api_error_status"] = api_error_status
    if sleep is not None:
        env["__sleep__"] = sleep
    return env


class _FakeClaudeCLI:
    """Context manager: writes a response sequence, points a fake `claude` executable at it via
    PATH, and cleans up. Enter returns the output dir the harness should write results into."""

    def __init__(self, responses):
        self.responses = responses

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="osw_vr_it_"))
        bindir = self.tmp / "bin"
        bindir.mkdir()
        shim = bindir / "claude"
        shim.write_text(f'#!/bin/sh\nexec python3 "{_FIXTURES / "fake_claude.py"}" "$@"\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        seq_path = self.tmp / "sequence.json"
        seq_path.write_text(json.dumps({"responses": self.responses}))
        counter_path = self.tmp / "counter.txt"

        self._real_path = os.environ.get("PATH", "")
        self._real_seq = os.environ.get("FAKE_CLAUDE_SEQUENCE")
        self._real_counter = os.environ.get("FAKE_CLAUDE_COUNTER")
        os.environ["PATH"] = f"{bindir}:{self._real_path}"
        os.environ["FAKE_CLAUDE_SEQUENCE"] = str(seq_path)
        os.environ["FAKE_CLAUDE_COUNTER"] = str(counter_path)
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()
        return self

    def call_count(self):
        try:
            return int((self.tmp / "counter.txt").read_text().strip())
        except FileNotFoundError:
            return 0

    def __exit__(self, *exc):
        os.environ["PATH"] = self._real_path
        for key, val in (("FAKE_CLAUDE_SEQUENCE", self._real_seq),
                         ("FAKE_CLAUDE_COUNTER", self._real_counter)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)


def _run(responses, task=None, timeouts=None):
    real = {k: getattr(config, k) for k in
            ("MODEL", "VR_INITIAL_TIMEOUT", "VR_AUDIT_TIMEOUT", "VR_RECOVERY_TIMEOUT",
             "VR_TOTAL_TIMEOUT", "VR_MAX_RECOVERIES", "VR_MIN_CONFIDENCE", "VR_AUDIT_ON_FAIL")}
    config.MODEL = "claude-sonnet-5"
    for k, v in (timeouts or {}).items():
        setattr(config, k, v)
    try:
        with _FakeClaudeCLI(responses) as cli:
            answer = verify_replan.run(task or _TASK, env=_FakeEnv(), out=cli.out_dir)
            result = json.loads((cli.out_dir / "result.json").read_text())
            eval_ = json.loads((cli.out_dir / "eval.json").read_text()) if \
                (cli.out_dir / "eval.json").exists() else None
            infra = json.loads((cli.out_dir / "infra_error.json").read_text()) if \
                (cli.out_dir / "infra_error.json").exists() else None
            calls = cli.call_count()
    finally:
        for k, v in real.items():
            setattr(config, k, v)
    return answer, result, eval_, infra, calls


# ---------------------------------------------------------------------------
# Scenario 1: executor done -> auditor verified -> no recovery
# ---------------------------------------------------------------------------

def test_scenario_executor_done_auditor_verified_no_recovery():
    answer, result, eval_, infra, calls = _run([
        _envelope(fixture="claim_done_valid.json"),
        _envelope(fixture="audit_verified_done.json"),
    ])
    assert answer == "DONE"
    assert result["harness"] == "verify_replan"
    assert result["initial_claim"] == "done"
    assert result["audit_initial"] == "verified_done"
    assert result["recovery_triggered"] is False
    assert result["recovery_attempts"] == 0
    assert infra is None
    assert calls == 2   # initial + audit_1, no recovery/final-audit calls made


# ---------------------------------------------------------------------------
# Scenario 2: executor done -> auditor not_done -> recovery -> verified
# ---------------------------------------------------------------------------

def test_scenario_false_completion_detected_and_recovered():
    answer, result, eval_, infra, calls = _run([
        _envelope(fixture="claim_done_valid.json"),      # initial
        _envelope(fixture="audit_not_done.json"),         # audit_1
        _envelope(fixture="claim_done_valid.json"),       # recovery_1
        _envelope(fixture="audit_verified_done.json"),    # audit_final
    ])
    assert result["recovery_triggered"] is True
    assert result["recovery_attempts"] == 1
    assert result["audit_initial"] == "not_done"
    assert result["audit_final"] == "verified_done"
    assert result["false_completion_detected"] is True
    assert result["false_completion_recovered"] is True
    assert set(result["role_usage"]) == {"initial", "audit_1", "recovery_1", "audit_final"}
    assert result["total_agent_cost_usd"] > result["role_usage"]["initial"]["agent_cost_usd"]
    assert calls == 4


# ---------------------------------------------------------------------------
# Scenario 3: executor infeasible -> auditor still runs by default (VR_AUDIT_ON_FAIL=1)
# ---------------------------------------------------------------------------

def test_scenario_infeasible_claim_is_still_audited_by_default():
    answer, result, eval_, infra, calls = _run([
        _envelope(fixture="claim_infeasible.json"),
        _envelope(fixture="audit_verified_done.json"),   # auditor agrees it's genuinely blocked
    ])
    assert result["initial_claim"] == "infeasible"
    assert calls == 2   # audit ran even though the claim was not "done"


def test_scenario_infeasible_claim_skips_audit_when_audit_on_fail_disabled():
    real = config.VR_AUDIT_ON_FAIL
    config.VR_AUDIT_ON_FAIL = False
    try:
        answer, result, eval_, infra, calls = _run([
            _envelope(fixture="claim_infeasible.json"),
        ])
    finally:
        config.VR_AUDIT_ON_FAIL = real
    assert result["audit_initial"] is None
    assert result["recovery_triggered"] is False
    assert calls == 1   # only the initial executor call -- no audit call made at all


# ---------------------------------------------------------------------------
# Scenario 4: auditor output corrupted -> audit_error, no recovery
# ---------------------------------------------------------------------------

def test_scenario_corrupted_audit_output_never_authorizes_recovery():
    answer, result, eval_, infra, calls = _run([
        _envelope(fixture="claim_done_valid.json"),
        _envelope(text=(_FIXTURES / "audit_corrupted.txt").read_text()),
    ])
    assert result["audit_initial"] == "audit_error"
    assert result["recovery_triggered"] is False


# ---------------------------------------------------------------------------
# Scenario 5: a slow call past its timeout degrades gracefully, does not hang or crash
# ---------------------------------------------------------------------------

def test_scenario_initial_executor_timeout_degrades_to_a_blocked_claim():
    """run_claude_meta returns {} on a timed-out subprocess (core.agent_loop._run_raw kills the
    whole process group and returns whatever was captured, which is nothing here) -- the harness
    must turn that into a well-formed "blocked" claim, not crash or hang the whole task."""
    answer, result, eval_, infra, calls = _run(
        [_envelope(text="", sleep=2)],
        timeouts={"VR_INITIAL_TIMEOUT": 0.3},
    )
    assert result["initial_claim"] == "blocked"
    assert infra is None   # a timed-out (not rate-limited) call still reaches scoring


# ---------------------------------------------------------------------------
# Scenario 6: rate limit at the initial executor, and separately at the auditor
# ---------------------------------------------------------------------------

def test_scenario_rate_limit_before_any_action_is_infra_not_a_verdict():
    answer, result, eval_, infra, calls = _run([
        _envelope(text="", api_error_status=429),
    ])
    assert answer == ""
    assert infra is not None and infra[-1]["outcome"] == "RATE_LIMITED"
    assert eval_ is None   # no eval.json written -- a throttled run is not a scored one
    assert calls == 1      # never reached the auditor


def test_scenario_rate_limited_auditor_scores_without_recovery():
    answer, result, eval_, infra, calls = _run([
        _envelope(fixture="claim_done_valid.json"),
        _envelope(text="", api_error_status=429),
    ])
    assert result["audit_initial"] == "audit_error"
    assert result["recovery_triggered"] is False
    assert infra is None   # the TASK still scores; only the auditor's own call was throttled


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
