"""Offline tests for scripts/g_official361_driver.py: the pure helpers (batching, the per-round
decision, resume), the protocol environment it exports, and --dry-run. No run.py, claude,
codex or docker is ever invoked -- the round runner is exercised with a fake Popen."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.osworld import config, tasks
from scripts import g_official361_driver as driver

_ROOT = Path(__file__).resolve().parents[3]


def _unit(task_id, run_idx, fresh=None, history=()):
    return {"task_id": task_id, "run_idx": run_idx, "fresh_outcome": fresh,
            "infra_history": list(history)}


def _batch(returncode, units):
    return {"returncode": returncode, "units": units}


# ---- batches ------------------------------------------------------------------------------

def test_batches_split_in_fixed_size_chunks_and_keep_order():
    ids = [f"t{i}" for i in range(12)]
    assert driver.batches(ids, 5) == [ids[0:5], ids[5:10], ids[10:12]]
    assert driver.batches([], 5) == []


# ---- decide -------------------------------------------------------------------------------

def test_decide_stops_when_a_batch_exits_nonzero_without_writing_infra_error():
    # e.g. the child's own preflight refused, or it crashed before reaching any unit
    results = [_batch(0, [_unit("a", 1)]), _batch(1, [_unit("b", 1), _unit("b", 2)])]
    assert driver.decide(results) == "stop"


def test_decide_does_not_stop_on_nonzero_exit_that_did_write_infra_error():
    results = [_batch(1, [_unit("b", 1, fresh="HARNESS_ERROR", history=["HARNESS_ERROR"]),
                          _unit("b", 2)])]
    assert driver.decide(results) == "continue"


def test_decide_backs_off_when_half_or_more_of_attempted_units_were_rate_limited():
    results = [_batch(0, [_unit("a", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("a", 2)]),
               _batch(0, [_unit("b", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("b", 2)])]
    assert driver.decide(results) == "backoff"


def test_decide_continues_below_the_rate_limit_threshold():
    results = [_batch(0, [_unit("a", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("a", 2), _unit("a", 3)])]
    assert driver.decide(results) == "continue"
    assert driver.decide([_batch(0, [_unit("a", 1), _unit("a", 2)])]) == "continue"


def test_decide_stops_on_a_unit_with_three_non_rate_limited_infra_errors():
    # Controller ruling: a unit that keeps failing for a non-quota reason would otherwise be
    # retried forever (no eval.json -> always pending). Rate limits never count toward this.
    stuck = _unit("s", 4, fresh="HARNESS_ERROR",
                  history=["INFRA_FLAKE", "RATE_LIMITED", "HARNESS_ERROR", "HARNESS_ERROR"])
    assert driver.decide([_batch(0, [stuck, _unit("s", 5)])]) == "stop"
    assert driver.stuck_units([_batch(0, [stuck])]) == [("s", 4)]
    only_quota = _unit("q", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"] * 5)
    two_harness = _unit("h", 1, fresh="HARNESS_ERROR", history=["HARNESS_ERROR"] * 2)
    assert driver.decide([_batch(0, [only_quota, two_harness])]) == "backoff"


# ---- pending_units ------------------------------------------------------------------------

def test_pending_units_skips_units_with_eval_json(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(tasks, "load_tasks", lambda: [{"id": "t1"}, {"id": "t2"}])
    done = tmp_path / "sys" / "t1" / "run_2"
    done.mkdir(parents=True)
    (done / "eval.json").write_text("{}")
    rate_limited = tmp_path / "sys" / "t2" / "run_1"   # infra_error only: still pending
    rate_limited.mkdir(parents=True)
    (rate_limited / "infra_error.json").write_text(json.dumps([{"outcome": "RATE_LIMITED"}]))
    assert driver.pending_units("sys", 2) == [("t1", 1), ("t2", 1), ("t2", 2)]


# ---- protocol environment -----------------------------------------------------------------

_COMMON = {"OSW_PROTOCOL": "official", "OSW_BACKEND": "kvm", "OSW_POPULATION": "verified361",
           "OSW_MAX_STEPS": "100", "OSW_SCREEN_WIDTH": "1920", "OSW_SCREEN_HEIGHT": "1080"}


def test_protocol_env_for_sonnet():
    assert driver.protocol_env("sonnet", {}) == {
        **_COMMON, "OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
        "OSW_MAX_OUTPUT_TOKENS": "128000", "OSW_SYSTEM_SUFFIX": "protocol361"}


def test_protocol_env_for_astra_requires_the_callers_effort():
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        driver.protocol_env("astra", {})
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        driver.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "  "})
    assert driver.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "xhigh"}) == {
        **_COMMON, "OSW_ASTRA_MODEL": "gpt-6-astra", "OSW_ASTRA_REASONING_EFFORT": "xhigh",
        "OSW_ASTRA_CAMPAIGN_LOCK": "astra_official361_lock.json",
        "OSW_ASTRA_SYSTEM_SUFFIX": "protocol361"}


def test_protocol_env_rejects_an_unknown_arm():
    with pytest.raises(SystemExit, match="ARM"):
        driver.protocol_env("gpt", {})


def test_importing_the_driver_does_not_import_config():
    # config reads the environment at import time, so the driver must export the protocol
    # environment BEFORE anything imports it (an earlier driver in this project got this wrong).
    code = ("import sys; import scripts.g_official361_driver; "
            "print('benchmarks.osworld.config' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(_ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


# ---- run_round (fake Popen) ---------------------------------------------------------------

class _FakePopen:
    """Stands in for a run.py child: writes the given per-unit infra outcomes into the results
    tree when started, and exits with `returncode`."""
    launched = []
    script = {}   # first task id of the batch -> (returncode, {(task, run): outcome})

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        _FakePopen.launched.append(cmd)
        ids = cmd[cmd.index("--ids") + 1:]
        self.returncode, outcomes = _FakePopen.script.get(ids[0], (0, {}))
        system = cmd[cmd.index("--system") + 1]
        for (tid, k), outcome in outcomes.items():
            out = config.RESULTS_DIR / system / tid / f"run_{k}"
            out.mkdir(parents=True, exist_ok=True)
            path = out / "infra_error.json"
            hist = json.loads(path.read_text()) if path.exists() else []
            path.write_text(json.dumps(hist + [{"outcome": outcome}]))

    def wait(self):
        return self.returncode


def test_run_round_launches_one_child_per_batch_and_reports_fresh_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(driver.subprocess, "Popen", _FakePopen)
    _FakePopen.launched = []
    stale = tmp_path / "sys" / "b" / "run_1"   # an older attempt: history, but not fresh
    stale.mkdir(parents=True)
    (stale / "infra_error.json").write_text(json.dumps([{"outcome": "INFRA_FLAKE"}]))
    _FakePopen.script = {"a": (0, {("a", 1): "RATE_LIMITED"}), "b": (2, {})}
    pending = [("a", 1), ("a", 2), ("b", 1)]
    results = driver.run_round("sys", [["a"], ["b"]], pending, runs=5, log_file=None)
    assert [c[c.index("--ids") + 1:] for c in _FakePopen.launched] == [["a"], ["b"]]
    for cmd in _FakePopen.launched:
        assert cmd[1:4] == ["-m", "benchmarks.osworld.run", "--system"]
        assert cmd[cmd.index("--runs") + 1] == "5"
        assert cmd[cmd.index("--concurrency") + 1] == "1"
        assert "--force" not in cmd
    assert results == [
        _batch(0, [_unit("a", 1, "RATE_LIMITED", ["RATE_LIMITED"]), _unit("a", 2)]),
        _batch(2, [_unit("b", 1, None, ["INFRA_FLAKE"])]),
    ]
    assert driver.decide(results) == "stop"   # b's child failed without writing anything


# ---- --dry-run ----------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_runs_nothing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"PYTHONPATH": str(_ROOT), "ARM": "sonnet", "PARALLEL": "3",
                "PATH": f"{fake_bin}:{env.get('PATH', '')}"})
    for name in ("claude", "codex", "docker"):   # would leave a marker if anything ran them
        exe = fake_bin / name
        exe.write_text(f"#!/bin/sh\ntouch {tmp_path}/{name}_ran\n")
        exe.chmod(0o755)
    out = subprocess.run([sys.executable, "scripts/g_official361_driver.py", "--dry-run"],
                         cwd=_ROOT, capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    plan = json.loads(out.stdout)
    assert plan["system"] == "agent_computer_sonnet5_effortmax_protocol361_official_kvm"
    assert plan["parallel"] == 3 and plan["runs"] == 5
    assert all(1 <= len(b) <= 5 for b in plan["batches"])
    assert len({t for b in plan["batches"] for t in b}) == len(sum(plan["batches"], []))
    assert not list(tmp_path.glob("*_ran"))


# ---- refusal of harness-altering knobs (pre-review fix 2) ----------------------------------

@pytest.mark.parametrize("name,value", [
    ("OSW_ZOOM_BATCH", "1"), ("OSW_GROUNDING", "1"), ("OSW_SELF_VERIFY", "1"),
    ("OSW_INLOOP_VERIFY", "1"), ("OSW_RESTRICT_RUN_PYTHON", "1"), ("OSW_ENFORCE_SANDBOX", "1"),
    ("OSW_OBSERVATION", "screenshot"), ("OSW_ACTION_SPACE", "computer_13"),
    ("OSW_MAX_TURNS", "100"), ("OSW_TASK_TIMEOUT", "3600"),
    ("OSW_SLEEP_AFTER_EXECUTION", "0"), ("OSW_POST_SETUP_WAIT_S", "0"),
    ("OSW_PRE_EVAL_WAIT_S", "0"), ("OSW_INCLUDE_ALL_APPS", "1"), ("OSW_PINNED_EVALUATORS", "0"),
    ("OSW_CONTROLLER_URL", "http://x:5000"), ("OSW_SANDBOX_ID", "sb-1"),
    ("OSW_RELEASE", "other"), ("OSW_ASTRA_CODEX_VERSION", "0.1.0"),
    ("OSW_POST_RUN_TIMEOUT", "60"),
    # the protocol's own values, set differently by the caller
    ("OSW_PROTOCOL", ""), ("OSW_BACKEND", "daytona"), ("OSW_POPULATION", "all"),
    ("OSW_MAX_STEPS", "30"), ("OSW_SCREEN_WIDTH", "1280"), ("OSW_SCREEN_HEIGHT", "720"),
    ("OSW_MODEL", "claude-opus-4-8"), ("OSW_EFFORT", "high"), ("OSW_SYSTEM_SUFFIX", "x"),
])
def test_env_conflicts_names_each_harness_altering_variable(name, value):
    conflicts = driver.env_conflicts("sonnet", {name: value})
    assert len(conflicts) == 1 and conflicts[0].startswith(name + "=")


def test_env_conflicts_accepts_defaults_protocol_values_and_host_settings():
    environ = {"OSW_ZOOM_BATCH": "0", "OSW_PINNED_EVALUATORS": "1", "OSW_MAX_TURNS": "150",
               "OSW_TASK_TIMEOUT": "", "OSW_OBSERVATION": "screenshot+a11y",
               "OSW_PROTOCOL": "official", "OSW_BACKEND": "kvm", "OSW_MAX_STEPS": "100",
               "OSW_KVM_ADDR": "10.0.0.2", "OSW_KVM_QCOW2": "/data/Ubuntu.qcow2",
               "OSW_ASTRA_REASONING_EFFORT": "xhigh"}
    assert driver.env_conflicts("sonnet", environ) == []
    assert driver.env_conflicts("astra", environ) == []
    assert driver.env_conflicts("astra", {**environ, "OSW_ASTRA_CAMPAIGN_LOCK": "a.json"}) == [
        "OSW_ASTRA_CAMPAIGN_LOCK='a.json' (the protocol requires "
        "'astra_official361_lock.json')"]


def test_main_refuses_with_exit_2_naming_every_conflicting_variable(monkeypatch, capsys):
    import core.dotenv
    monkeypatch.setattr(core.dotenv, "load_dotenv", lambda: None)
    monkeypatch.setenv("ARM", "sonnet")
    monkeypatch.setenv("OSW_ZOOM_BATCH", "1")
    monkeypatch.setenv("OSW_MAX_STEPS", "30")
    assert driver.main(["--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "OSW_ZOOM_BATCH" in err and "OSW_MAX_STEPS" in err


def test_main_checks_knobs_that_come_from_the_repo_dotenv(monkeypatch, capsys):
    # core.run.main loads the repo-root .env in every child, so a knob set only there would
    # otherwise reach the children unchecked.
    import core.dotenv
    monkeypatch.setattr(core.dotenv, "load_dotenv",
                        lambda: monkeypatch.setenv("OSW_GROUNDING", "1"))
    monkeypatch.setenv("ARM", "sonnet")
    monkeypatch.delenv("OSW_GROUNDING", raising=False)
    assert driver.main(["--dry-run"]) == 2
    assert "OSW_GROUNDING" in capsys.readouterr().err


# ---- signal handling (pre-review fix 3) ----------------------------------------------------

def _sleeper(ignore_term=False):
    code = ("import signal, time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "print('up', flush=True)\ntime.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "up"   # handler installed before we signal it
    return p


def test_terminate_children_sends_sigterm_then_kills_after_the_grace_period():
    polite, stubborn = _sleeper(), _sleeper(ignore_term=True)
    finished = _sleeper()
    finished.kill()
    finished.wait()
    lines = []
    terminated, killed = driver.terminate_children([polite, stubborn, finished], grace_s=1,
                                                   log=lines.append)
    assert (terminated, killed) == (2, 1)
    assert polite.poll() == -15 and stubborn.poll() == -9
    assert lines


_DRIVER_UNDER_SIGNAL = """
import os, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import scripts.g_official361_driver as d
tmp = Path(sys.argv[1])
fb = types.ModuleType("benchmarks.osworld.benchmark")
class _Runners(dict):
    def __missing__(self, k):
        return types.SimpleNamespace(preflight=lambda: None)
fb.build = lambda: types.SimpleNamespace(runners=_Runners())
sys.modules["benchmarks.osworld.benchmark"] = fb
d._ROOT = tmp
(tmp / "scripts").mkdir()
d.pending_units = lambda system, runs: [(f"t{i}", 1) for i in range(10)]
d.CHILD_GRACE_S = 2
_real = subprocess.Popen
n = [0]
def fake_popen(cmd, **kw):   # a fake run.py child: sleeps; the second one ignores SIGTERM
    n[0] += 1
    code = ("import os, signal, sys, time\\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if n[0] == 2 else "")
            + "open(sys.argv[1], 'w').write(str(os.getpid()))\\ntime.sleep(120)\\n")
    return _real([sys.executable, "-c", code, str(tmp / f"child{n[0]}.pid")])
d.subprocess.Popen = fake_popen
sys.exit(d.main([]))
"""


def test_sigterm_to_the_driver_terminates_its_children_and_exits_nonzero(tmp_path):
    import signal
    import time
    helper = tmp_path / "helper.py"
    helper.write_text(_DRIVER_UNDER_SIGNAL)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"ARM": "sonnet", "PARALLEL": "2"})
    proc = subprocess.Popen([sys.executable, str(helper), str(tmp_path), str(_ROOT)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pid_files = [tmp_path / "child1.pid", tmp_path / "child2.pid"]
    deadline = time.time() + 30
    while not all(p.exists() and p.read_text() for p in pid_files):
        assert time.time() < deadline and proc.poll() is None, proc.communicate()[0]
        time.sleep(0.1)
    time.sleep(0.5)   # let the stubborn child install its SIG_IGN handler
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM, out
    for p in pid_files:
        with pytest.raises(ProcessLookupError):
            os.kill(int(p.read_text()), 0)
    log = (tmp_path / "scripts" / "g_official361_sonnet.log").read_text()
    assert "SIGTERM" in log and "1 killed" in log
