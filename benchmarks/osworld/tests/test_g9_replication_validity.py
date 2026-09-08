"""Unit tests for g9_replication_validity.py (offline, no sandbox, synthetic raw data):
  python -m benchmarks.osworld.tests.test_g9_replication_validity
"""
import json
import tempfile
from pathlib import Path

from benchmarks.osworld.analysis import g9_replication_validity as g9

BROKEN = {"get_vm_command_line"}


def _task(tid, getter="vm_file"):
    return {"id": tid, "related_apps": ["libreoffice_calc"],
            "evaluator": {"func": "exact_match", "result": {"type": getter}}}


def _write_run(base, tid, run_idx, verdict=None, reason="", **result):
    d = base / "agent_computer" / tid / f"run_{run_idx}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps({"id": tid, **result}))
    if verdict:
        (d / "eval.json").write_text(json.dumps({"id": tid, "verdict": verdict,
                                                 "reason": reason}))
    return d


def _audit(base, tasks):
    return g9.audit(results_dir=base, system="agent_computer",
                    tasks_by_id={t["id"]: t for t in tasks}, broken_getters=BROKEN)


def _tmp():
    return Path(tempfile.mkdtemp(prefix="osw_g9_"))


def test_a_clean_scored_run_stays_valid():
    d = _tmp()
    _write_run(d, "clean", 1, "SUCCESS")
    r = _audit(d, [_task("clean")])
    assert r["buckets"] == {"VALID": 1}
    assert r["rerun_task_ids"] == []


def test_a_failure_through_a_broken_getter_that_was_really_called_is_not_the_agents_fault():
    """The dangerous case: the getter is reached, returns nothing usable, the metric scores the
    emptiness 0.0, and the run is filed as the agent losing."""
    d = _tmp()
    _write_run(d, "silent", 1, "FAILURE", answer="DONE")
    r = _audit(d, [_task("silent", getter="vm_command_line")])
    assert r["causes"]["oracle_getter_reached_zero"]["runs"] == 1
    assert r["rerun_task_ids"] == ["silent"]


def test_a_fail_answer_short_circuits_before_the_getter_so_the_verdict_stands():
    """evaluate_official returns 0.0 as soon as the agent's own last answer is FAIL, before any
    getter runs. Reading that 0.0 as an oracle artifact would queue good tasks for a re-run --
    which is exactly the mistake this test exists to prevent."""
    d = _tmp()
    _write_run(d, "gaveup", 1, "FAILURE", answer="FAIL")
    r = _audit(d, [_task("gaveup", getter="vm_command_line")])
    assert r["buckets"] == {"VALID": 1}
    assert r["rerun_task_ids"] == []


def test_a_truncated_agent_is_invalid_however_the_oracle_scored_it():
    d = _tmp()
    _write_run(d, "cut", 1, "FAILURE", agent_api_error_status=429, agent_is_error=True)
    r = _audit(d, [_task("cut")])
    assert r["causes"]["agent_truncated"]["runs"] == 1


def test_a_rate_limited_run_that_was_never_scored_is_unscored_not_invalid():
    """96 of the campaign's 97 rate-limited runs wrote no eval.json at all -- resume retries
    them, and they must not inflate the invalid count."""
    d = _tmp()
    _write_run(d, "stub", 1, agent_api_error_status=429, agent_is_error=True)
    r = _audit(d, [_task("stub")])
    assert r["n_runs_scored"] == 0 and r["n_runs_unscored"] == 1
    assert r["rerun_task_ids"] == []


def test_library_version_skew_outranks_the_url_scheme_signature():
    """An AttributeError on a task that also uses a broken getter is a pinning problem, not
    the scheme bug -- misfiling it would send the wrong fix."""
    d = _tmp()
    _write_run(d, "skew", 1, "EVAL_ERROR",
               "official eval errored: AttributeError: module "
               "'desktop_env.evaluators.metrics' has no attribute 'compare_x'")
    r = _audit(d, [_task("skew", getter="vm_command_line")])
    assert r["causes"]["oracle_lib_version"]["runs"] == 1
    assert "oracle_url_scheme" not in r["causes"]


def test_credentials_and_upstream_rot_are_excluded_not_re_runnable():
    d = _tmp()
    _write_run(d, "creds", 1, "ENVIRONMENT_ERROR",
               "config setup failed: _googledrive_setup - Invalid client secrets file")
    _write_run(d, "gone", 1, "ENVIRONMENT_ERROR",
               "config setup failed: _open_setup - 404 Client Error: Not Found")
    _write_run(d, "flake", 1, "ENVIRONMENT_ERROR",
               "config setup failed: _chrome_open_tabs_setup - read ECONNRESET")
    r = _audit(d, [_task("creds"), _task("gone"), _task("flake")])
    assert r["excluded_task_ids"] == ["creds", "gone"]
    assert r["rerun_task_ids"] == ["flake"]     # transient: re-running is the whole remedy


def test_a_missing_state_crash_is_unresolved_until_a_live_probe_says_otherwise():
    d = _tmp()
    _write_run(d, "vlc", 1, "EVAL_ERROR",
               "official eval errored: TypeError: a bytes-like object is required, "
               "not 'NoneType'")
    r = _audit(d, [_task("vlc")])
    assert r["unresolved_task_ids"] == ["vlc"]
    assert r["rerun_task_ids"] == []            # re-running proves nothing while ambiguous


def test_affected_getters_is_derived_from_source_not_hardcoded():
    d = _tmp()
    g = d / "evaluators" / "getters"
    g.mkdir(parents=True)
    (g / "general.py").write_text(
        'def get_bad(env, config):\n    requests.post(f"http://{vm_ip}:{port}/execute")\n\n'
        'def get_good(env, config):\n    return env.controller.get_file("/x")\n')
    assert g9.affected_getters(package_dir=d) == {"get_bad"}


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
        print("ok", name)
