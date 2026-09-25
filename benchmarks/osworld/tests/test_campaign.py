"""Offline tests for the backend-neutral campaign core (benchmarks/osworld/campaign.py): the
pure helpers (batching, the per-round decision, resume), the protocol environment it exports
together with a backend, and --dry-run. No run.py, claude, codex, docker or any real backend is
ever invoked -- the round runner is exercised with a fake Popen, and every backend-shaped
argument is a FakeBackend below. KVM-specific behavior (the container sweep itself, the pinned
kvm image check, a docker error during the sweep, and the kvm-teardown grace-period test) stays
in the kvm backend's own tests."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.osworld import config, tasks
from benchmarks.osworld import campaign

_ROOT = Path(__file__).resolve().parents[3]


def _unit(task_id, run_idx, fresh=None, history=()):
    return {"task_id": task_id, "run_idx": run_idx, "fresh_outcome": fresh,
            "infra_history": list(history)}


def _batch(returncode, units):
    return {"returncode": returncode, "units": units}


class FakeBackend:
    NAME = "fake"
    LOG_SUFFIX = "_fake"

    def __init__(self):
        self.swept = []

    def protocol_env(self):
        return {"OSW_BACKEND": "daytona"}

    def harness_knob_defaults(self):
        return {"OSW_FAKE_KNOB": "0"}

    def env_conflicts(self, environ):
        return ["OSW_FAKE_BAD='1' (fake refusal)"] if environ.get("OSW_FAKE_BAD") else []

    def sweep(self, run_id, log):
        if not run_id:
            raise ValueError("empty driver run id")
        self.swept.append(run_id)
        return 0


# ---- batches ------------------------------------------------------------------------------

def test_batches_split_in_fixed_size_chunks_and_keep_order():
    ids = [f"t{i}" for i in range(12)]
    assert campaign.batches(ids, 5) == [ids[0:5], ids[5:10], ids[10:12]]
    assert campaign.batches([], 5) == []


# ---- decide -------------------------------------------------------------------------------

def test_decide_stops_when_a_batch_exits_nonzero_without_writing_infra_error():
    # e.g. the child's own preflight refused, or it crashed before reaching any unit
    results = [_batch(0, [_unit("a", 1)]), _batch(1, [_unit("b", 1), _unit("b", 2)])]
    assert campaign.decide(results) == "stop"


def test_decide_does_not_stop_on_nonzero_exit_that_did_write_infra_error():
    results = [_batch(1, [_unit("b", 1, fresh="HARNESS_ERROR", history=["HARNESS_ERROR"]),
                          _unit("b", 2)])]
    assert campaign.decide(results) == "continue"


def test_decide_backs_off_when_half_or_more_of_attempted_units_were_rate_limited():
    results = [_batch(0, [_unit("a", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("a", 2)]),
               _batch(0, [_unit("b", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("b", 2)])]
    assert campaign.decide(results) == "backoff"


def test_decide_continues_below_the_rate_limit_threshold():
    results = [_batch(0, [_unit("a", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]),
                          _unit("a", 2), _unit("a", 3)])]
    assert campaign.decide(results) == "continue"
    assert campaign.decide([_batch(0, [_unit("a", 1), _unit("a", 2)])]) == "continue"


def test_decide_stops_on_a_unit_with_three_non_rate_limited_infra_errors():
    # Controller ruling: a unit that keeps failing for a non-quota reason would otherwise be
    # retried forever (no eval.json -> always pending). Rate limits never count toward this.
    stuck = _unit("s", 4, fresh="HARNESS_ERROR",
                  history=["INFRA_FLAKE", "RATE_LIMITED", "HARNESS_ERROR", "HARNESS_ERROR"])
    assert campaign.decide([_batch(0, [stuck, _unit("s", 5)])]) == "stop"
    assert campaign.stuck_units([_batch(0, [stuck])]) == [("s", 4)]
    only_quota = _unit("q", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"] * 5)
    two_harness = _unit("h", 1, fresh="HARNESS_ERROR", history=["HARNESS_ERROR"] * 2)
    assert campaign.decide([_batch(0, [only_quota, two_harness])]) == "backoff"


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
    assert campaign.pending_units("sys", 2) == [("t1", 1), ("t2", 1), ("t2", 2)]


# ---- protocol environment -----------------------------------------------------------------

_COMMON = {"OSW_PROTOCOL": "official", "OSW_BACKEND": "daytona", "OSW_POPULATION": "verified361",
           "OSW_MAX_STEPS": "100", "OSW_SCREEN_WIDTH": "1920", "OSW_SCREEN_HEIGHT": "1080"}


def test_protocol_env_for_sonnet():
    assert campaign.protocol_env("sonnet", {}, FakeBackend()) == {
        **_COMMON, "OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
        "OSW_MAX_OUTPUT_TOKENS": "128000", "OSW_SYSTEM_SUFFIX": "protocol361"}


def test_protocol_env_for_astra_requires_the_callers_effort():
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        campaign.protocol_env("astra", {}, FakeBackend())
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        campaign.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "  "}, FakeBackend())
    assert campaign.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "xhigh"},
                                 FakeBackend()) == {
        **_COMMON, "OSW_ASTRA_MODEL": "gpt-6-astra", "OSW_ASTRA_REASONING_EFFORT": "xhigh",
        "OSW_ASTRA_CAMPAIGN_LOCK": "astra_official361_lock.json",
        "OSW_ASTRA_SYSTEM_SUFFIX": "protocol361"}


def test_protocol_env_rejects_an_unknown_arm():
    with pytest.raises(SystemExit, match="ARM"):
        campaign.protocol_env("gpt", {}, FakeBackend())


def test_importing_the_core_does_not_import_config():
    code = ("import sys; import benchmarks.osworld.campaign; "
            "sys.exit(1 if 'benchmarks.osworld.config' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=_ROOT).returncode == 0


def test_backend_knobs_and_conflicts_join_the_core_checks():
    b = FakeBackend()
    out = campaign.env_conflicts("sonnet", {"OSW_FAKE_KNOB": "1", "OSW_FAKE_BAD": "1"}, b)
    assert any(c.startswith("OSW_FAKE_KNOB='1'") for c in out)
    assert "OSW_FAKE_BAD='1' (fake refusal)" in out
    env = campaign.child_env("sonnet", {}, b)
    assert env["OSW_FAKE_KNOB"] == "0" and env["OSW_BACKEND"] == "daytona"
    assert "OSW_KVM_CLIENT_PASSWORD" not in campaign._HARNESS_KNOB_DEFAULTS


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
            if outcome == "EVAL":
                (out / "eval.json").write_text("{}")
                continue
            if outcome == "GARBAGE":
                path.write_text("{not json")
                continue
            hist = json.loads(path.read_text()) if path.exists() else []
            path.write_text(json.dumps(hist + [{"outcome": outcome}]))

    def wait(self):
        return self.returncode


def test_run_round_launches_one_child_per_batch_and_reports_fresh_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(campaign.subprocess, "Popen", _FakePopen)
    _FakePopen.launched = []
    stale = tmp_path / "sys" / "b" / "run_1"   # an older attempt: history, but not fresh
    stale.mkdir(parents=True)
    (stale / "infra_error.json").write_text(json.dumps([{"outcome": "INFRA_FLAKE"}]))
    _FakePopen.script = {"a": (0, {("a", 1): "RATE_LIMITED"}), "b": (2, {})}
    pending = [("a", 1), ("a", 2), ("b", 1)]
    results = campaign.run_round("sys", [["a"], ["b"]], pending, runs=5, log_file=None)
    assert [c[c.index("--ids") + 1:] for c in _FakePopen.launched] == [["a"], ["b"]]
    for cmd in _FakePopen.launched:
        assert cmd[1:4] == ["-m", "benchmarks.osworld.run", "--system"]
        assert cmd[cmd.index("--runs") + 1] == "5"
        assert cmd[cmd.index("--concurrency") + 1] == "1"
        assert "--force" not in cmd
    no_outcome = lambda u, n: {**u, "no_outcome": True, "no_outcomes": n}
    outcome = lambda u: {**u, "no_outcome": False, "no_outcomes": 0}
    assert results == [
        _batch(0, [outcome(_unit("a", 1, "RATE_LIMITED", ["RATE_LIMITED"])),
                   no_outcome(_unit("a", 2), 1)]),
        _batch(2, [no_outcome(_unit("b", 1, None, ["INFRA_FLAKE"]), 1)]),
    ]
    assert campaign.decide(results) == "stop"   # b's child failed without writing anything


# ---- --dry-run ----------------------------------------------------------------------------

_FAKE_BACKEND_SCRIPT = """
class B:
    NAME = 'fake'
    LOG_SUFFIX = '_fake'
    def protocol_env(self):
        return {'OSW_BACKEND': 'daytona'}
    def harness_knob_defaults(self):
        return {}
    def env_conflicts(self, environ):
        return []
    def sweep(self, run_id, log):
        return 0
"""


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
    code = (_FAKE_BACKEND_SCRIPT + "\nimport sys\n"
            "from benchmarks.osworld import campaign\n"
            "sys.exit(campaign.main(B(), ['--dry-run']))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True,
                         text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    plan = json.loads(out.stdout)
    assert plan["system"] == "agent_computer_sonnet5_effortmax_protocol361_official"
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
    ("OSW_PROTOCOL", ""), ("OSW_BACKEND", "kvm"), ("OSW_POPULATION", "all"),
    ("OSW_MAX_STEPS", "30"), ("OSW_SCREEN_WIDTH", "1280"), ("OSW_SCREEN_HEIGHT", "720"),
    ("OSW_MODEL", "claude-opus-4-8"), ("OSW_EFFORT", "high"), ("OSW_SYSTEM_SUFFIX", "x"),
])
def test_env_conflicts_names_each_harness_altering_variable(name, value):
    conflicts = campaign.env_conflicts("sonnet", {name: value}, FakeBackend())
    assert len(conflicts) == 1 and conflicts[0].startswith(name + "=")


def test_env_conflicts_accepts_defaults_protocol_values_and_host_settings():
    environ = {"OSW_ZOOM_BATCH": "0", "OSW_PINNED_EVALUATORS": "1", "OSW_MAX_TURNS": "150",
               "OSW_TASK_TIMEOUT": "", "OSW_OBSERVATION": "screenshot+a11y",
               "OSW_PROTOCOL": "official", "OSW_BACKEND": "daytona", "OSW_MAX_STEPS": "100",
               "OSW_ASTRA_REASONING_EFFORT": "xhigh"}
    assert campaign.env_conflicts("sonnet", environ, FakeBackend()) == []
    assert campaign.env_conflicts("astra", environ, FakeBackend()) == []
    assert campaign.env_conflicts("astra", {**environ, "OSW_ASTRA_CAMPAIGN_LOCK": "a.json"},
                                  FakeBackend()) == [
        "OSW_ASTRA_CAMPAIGN_LOCK='a.json' (the protocol requires "
        "'astra_official361_lock.json')"]


def test_main_refuses_with_exit_2_naming_every_conflicting_variable(monkeypatch, capsys):
    import core.dotenv
    monkeypatch.setattr(core.dotenv, "load_dotenv", lambda: None)
    monkeypatch.setenv("ARM", "sonnet")
    monkeypatch.setenv("OSW_ZOOM_BATCH", "1")
    monkeypatch.setenv("OSW_MAX_STEPS", "30")
    assert campaign.main(FakeBackend(), ["--dry-run"]) == 2
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
    assert campaign.main(FakeBackend(), ["--dry-run"]) == 2
    assert "OSW_GROUNDING" in capsys.readouterr().err


def test_driver_knob_table_agrees_with_config_legacy_neutral_values():
    for name, neutral in campaign._HARNESS_KNOB_DEFAULTS.items():
        if name in config._LEGACY_KNOB_NEUTRAL:
            assert config._LEGACY_KNOB_NEUTRAL[name] == neutral, name
    # Pin the one legacy knob the driver's own table doesn't carry (it never sets it, so it has
    # nothing to agree on): without this, the loop above silently checks nothing if the two
    # tables ever stopped overlapping at all.
    assert set(config._LEGACY_KNOB_NEUTRAL) - set(campaign._HARNESS_KNOB_DEFAULTS) == \
        {"OSW_OPENBOOK_IMAGE"}


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
    terminated, killed = campaign.terminate_children([polite, stubborn, finished], grace_s=1,
                                                      log=lines.append)
    assert (terminated, killed) == (2, 1)
    assert polite.poll() == -15 and stubborn.poll() == -9
    assert lines


_CAMPAIGN_UNDER_TEST = """
import json, os, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import benchmarks.osworld.campaign as d
tmp, mode = Path(sys.argv[1]), sys.argv[3]
fb = types.ModuleType("benchmarks.osworld.benchmark")
class _Runners(dict):
    def __missing__(self, k):   # records the probe marker the in-process preflight saw
        return types.SimpleNamespace(preflight=lambda: (tmp / "preflight_ok.txt").write_text(
            os.environ.get("OSW_OFFICIAL_PREFLIGHT_OK", "")))
fb.build = lambda: types.SimpleNamespace(runners=_Runners())
sys.modules["benchmarks.osworld.benchmark"] = fb
d._ROOT = tmp
(tmp / "scripts").mkdir()
if mode == "prestuck":   # t0#1 already failed 3x (non-quota) in an earlier session
    d._infra_history = lambda s, t, k: ["HARNESS_ERROR"] * 3 if (t, k) == ("t0", 1) else []
if mode == "done":
    d.pending_units = lambda system, runs: []
else:
    d.pending_units = lambda system, runs: [(f"t{i}", 1) for i in range(10)]
d.CHILD_GRACE_S = 2


class _Backend:   # fake backend: records the run id it was asked to sweep
    NAME = "fake"
    LOG_SUFFIX = "_fake"

    def protocol_env(self):
        return {"OSW_BACKEND": "daytona"}

    def harness_knob_defaults(self):
        return {}

    def env_conflicts(self, environ):
        return []

    def sweep(self, run_id, log):
        (tmp / "sweep.json").write_text(json.dumps({"run_id": run_id}))
        log(f"backend sweep: removed 1 sandbox(es) for driver_run={run_id}")
        return 1


backend = _Backend()

_real = subprocess.Popen
n = [0]
if mode == "grandchild":   # a fake run.py that is a real core.run.main (see _GRANDCHILD_RUN_PY)
    d.pending_units = lambda system, runs: [("t0", 1)]
    d.subprocess.Popen = lambda cmd, **kw: _real(
        [sys.executable, str(tmp / "run_py.py"), sys.argv[2], str(tmp)], **kw)
    sys.exit(d.main(backend, []))
def fake_popen(cmd, **kw):   # a fake run.py child: sleeps; the second one ignores SIGTERM
    n[0] += 1
    code = ("import os, signal, sys, time\\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if n[0] == 2 else "")
            + "open(sys.argv[1] + '.run', 'w').write(os.environ.get('OSW_DRIVER_RUN', ''))\\n"
            + "open(sys.argv[1] + '.ok', 'w').write("
            + "os.environ.get('OSW_OFFICIAL_PREFLIGHT_OK', ''))\\n"
            # what run.py's core.run.main does first: load the repo .env with setdefault
            + "sys.path.insert(0, sys.argv[2]); from core.dotenv import load_dotenv\\n"
            + "load_dotenv(sys.argv[3])\\n"
            + "import json; open(sys.argv[1] + '.env', 'w').write(json.dumps("
            + "{k: os.environ.get(k) for k in ('OSW_ZOOM_BATCH', 'OSW_TASK_TIMEOUT')}))\\n"
            + "open(sys.argv[1], 'w').write(str(os.getpid()))\\ntime.sleep(120)\\n")
    (tmp / "dotenv").write_text("OSW_ZOOM_BATCH=1\\nOSW_TASK_TIMEOUT=60\\n")
    return _real([sys.executable, "-c", code, str(tmp / f"child{n[0]}.pid"), sys.argv[2],
                  str(tmp / "dotenv")])
d.subprocess.Popen = fake_popen
sys.exit(d.main(backend, []))
"""


def _start_driver(tmp_path, mode):
    helper = tmp_path / "helper.py"
    helper.write_text(_CAMPAIGN_UNDER_TEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    # a stale probe marker from the caller's shell must never reach the preflight (V2)
    env.update({"ARM": "sonnet", "PARALLEL": "2", "OSW_OFFICIAL_PREFLIGHT_OK": "stale"})
    return subprocess.Popen([sys.executable, str(helper), str(tmp_path), str(_ROOT), mode],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _signal_driver_once_children_run(tmp_path, proc):
    import signal
    import time
    pid_files = [tmp_path / "child1.pid", tmp_path / "child2.pid"]
    deadline = time.time() + 30
    while not all(p.exists() and p.read_text() for p in pid_files):
        assert time.time() < deadline and proc.poll() is None, proc.communicate()[0]
        time.sleep(0.1)
    time.sleep(0.5)   # let the stubborn child install its SIG_IGN handler
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    return pid_files, out


def test_sigterm_terminates_the_children_sweeps_this_runs_orphans_and_exits_nonzero(tmp_path):
    import signal
    proc = _start_driver(tmp_path, "signal")
    pid_files, out = _signal_driver_once_children_run(tmp_path, proc)
    assert proc.returncode == 128 + signal.SIGTERM, out
    for p in pid_files:
        with pytest.raises(ProcessLookupError):
            os.kill(int(p.read_text()), 0)
    # every child got the same per-driver-run id, and the sweep was asked for exactly that run id
    run_ids = {(tmp_path / f"child{i}.pid.run").read_text() for i in (1, 2)}
    assert len(run_ids) == 1 and len(run_ids.pop()) == 32
    run_id = (tmp_path / "child1.pid.run").read_text()
    assert json.loads((tmp_path / "sweep.json").read_text()) == {"run_id": run_id}
    log = (tmp_path / "scripts" / "g_official361_sonnet_fake.log").read_text()
    assert "SIGTERM" in log and "1 killed" in log and "backend sweep: removed 1 sandbox" in log
    # every harness knob reaches the child pinned at its default, so the repo .env's setdefault
    # can no longer inject one mid-campaign (minor 4)
    assert json.loads((tmp_path / "child1.pid.env").read_text()) == {
        "OSW_ZOOM_BATCH": "0", "OSW_TASK_TIMEOUT": ""}


def test_a_unit_already_stuck_from_an_earlier_session_stops_before_any_vm_starts(tmp_path):
    proc = _start_driver(tmp_path, "prestuck")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 3, out
    assert not (tmp_path / "child1.pid").exists()
    log = (tmp_path / "scripts" / "g_official361_sonnet_fake.log").read_text()
    assert "STOP" in log and "t0#1" in log


def test_the_sweep_also_runs_on_normal_exit(tmp_path):
    proc = _start_driver(tmp_path, "done")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert "run_id" in json.loads((tmp_path / "sweep.json").read_text())
    assert "backend sweep: removed 1 sandbox" in (
        tmp_path / "scripts" / "g_official361_sonnet_fake.log").read_text()


def test_child_env_pins_every_harness_knob_at_its_default():
    env = campaign.child_env("sonnet", {}, FakeBackend())
    assert env == {**campaign._HARNESS_KNOB_DEFAULTS, "OSW_FAKE_KNOB": "0",
                   **campaign.protocol_env("sonnet", {}, FakeBackend())}
    assert env["OSW_TASK_TIMEOUT"] == "" and env["OSW_ZOOM_BATCH"] == "0"


def test_stuck_pending_finds_units_stuck_in_an_earlier_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    for (tid, k), hist in {("a", 1): ["HARNESS_ERROR", "INFRA_FLAKE", "HARNESS_ERROR"],
                           ("b", 1): ["RATE_LIMITED"] * 4 + ["HARNESS_ERROR"] * 2}.items():
        out = tmp_path / "sys" / tid / f"run_{k}"
        out.mkdir(parents=True)
        (out / "infra_error.json").write_text(json.dumps([{"outcome": o} for o in hist]))
    assert campaign.stuck_pending("sys", [("a", 1), ("b", 1), ("c", 1)]) == [("a", 1)]


def test_a_unit_that_ends_with_no_outcome_counts_toward_the_stuck_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(campaign.subprocess, "Popen", _FakePopen)
    _FakePopen.launched = []
    # a#1 scored; a#2 wrote nothing; a#3 left an unparsable infra_error.json
    _FakePopen.script = {"a": (0, {("a", 1): "EVAL", ("a", 3): "GARBAGE"})}
    no_outcomes = {("a", 2): 2}   # two earlier rounds of this session already ended that way
    results = campaign.run_round("sys", [["a"]], [("a", 1), ("a", 2), ("a", 3)], runs=5,
                                 log_file=None, no_outcomes=no_outcomes)
    units = {(u["task_id"], u["run_idx"]): u for u in results[0]["units"]}
    assert not units[("a", 1)]["no_outcome"]
    assert units[("a", 2)]["no_outcome"] and units[("a", 2)]["no_outcomes"] == 3
    assert units[("a", 3)]["no_outcome"] and units[("a", 3)]["no_outcomes"] == 1
    assert no_outcomes == {("a", 2): 3, ("a", 3): 1}
    assert campaign.stuck_units(results) == [("a", 2)]
    assert campaign.decide(results) == "stop"
    # rate limits never count, a no-outcome does
    rl = _unit("q", 1, fresh=None, history=["HARNESS_ERROR", "RATE_LIMITED", "HARNESS_ERROR"])
    assert campaign.stuck_units([_batch(0, [{**rl, "no_outcomes": 1}])]) == [("q", 1)]
    assert campaign.stuck_units([_batch(0, [rl])]) == []


# ---- fix round 2 (after task 10b: core.run reaps agent groups on interrupt) -----------------

def test_interrupted_records_never_count_toward_the_stuck_rule(tmp_path, monkeypatch):
    # an operator/driver stop is not a unit failure, exactly like a rate limit
    interrupted = ["INTERRUPTED", "HARNESS_ERROR", "INTERRUPTED", "HARNESS_ERROR", "INTERRUPTED"]
    assert campaign.stuck_units([_batch(0, [_unit("i", 1, "INTERRUPTED", interrupted)])]) == []
    assert campaign.stuck_units([_batch(0, [_unit("i", 1, "HARNESS_ERROR",
                                                   interrupted + ["HARNESS_ERROR"])])]) == \
        [("i", 1)]
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    out = tmp_path / "sys" / "i" / "run_1"
    out.mkdir(parents=True)
    (out / "infra_error.json").write_text(json.dumps([{"outcome": o} for o in interrupted]))
    assert campaign.stuck_pending("sys", [("i", 1)]) == []


def test_driver_run_id_is_not_on_the_refuse_list():
    environ = {"OSW_DRIVER_RUN": "abc"}
    assert campaign.env_conflicts("sonnet", environ, FakeBackend()) == []


def test_the_backend_sweep_runs_after_every_round_and_on_exit(tmp_path, monkeypatch):
    """Drive _campaign through two rounds with a fake run_round and preflight; the fake
    backend's sweep must see the driver run id after each round and once more on exit."""
    import types
    import core.dotenv
    from benchmarks.osworld import benchmark as benchmark_module

    monkeypatch.setattr(core.dotenv, "load_dotenv", lambda: None)
    monkeypatch.setenv("ARM", "sonnet")

    plans = [["t1#1"], ["t1#1"], []]   # round 1 pending, round 2 pending, then done

    def fake_pending_units(system, runs):
        return [("t1", 1)] if plans.pop(0) else []

    def fake_run_round(system, round_batches, pending, runs, log_file, no_outcomes=None):
        return [_batch(0, [{**_unit("t1", 1), "no_outcome": False, "no_outcomes": 0}])]

    class _Runners(dict):
        def __missing__(self, k):
            return types.SimpleNamespace(preflight=lambda: None)

    monkeypatch.setattr(campaign, "pending_units", fake_pending_units)
    monkeypatch.setattr(campaign, "run_round", fake_run_round)
    monkeypatch.setattr(benchmark_module, "build",
                        lambda: types.SimpleNamespace(runners=_Runners()))
    monkeypatch.setattr(campaign, "_ROOT", tmp_path)
    (tmp_path / "scripts").mkdir()
    # This test module imports benchmarks.osworld.config at the top for its own fixtures; main()
    # itself must never see it already imported, exactly like a fresh driver process wouldn't.
    monkeypatch.delitem(sys.modules, "benchmarks.osworld.config", raising=False)

    before_env = dict(os.environ)
    b = FakeBackend()
    try:
        assert campaign.main(b, []) == 0
        run_id = os.environ["OSW_DRIVER_RUN"]
        assert b.swept == [run_id, run_id, run_id]
    finally:
        os.environ.clear()
        os.environ.update(before_env)


_GRANDCHILD_RUN_PY = """
import os, sys
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core.agent_loop import _run_raw
from core.environment import Env
from core.judge import Judge
from core.run import Benchmark, Runner, main
tmp = Path(sys.argv[2])
open(tmp / "child.pid", "w").write(str(os.getpid()))


@contextmanager
def env_cm(task, *, port=None):
    try:
        yield Env(port=None)
    finally:
        (tmp / "teardown_ran").write_text("1")


def run_fn(task, *, env, out, refs=None, dry=False):
    # the real spawner path: its own session (start_new_session), registered in core.procgroups
    code = ("import os, time; open(" + repr(str(tmp / "grandchild.pid"))
            + ", 'w').write(str(os.getpid())); time.sleep(120)")
    _run_raw([sys.executable, "-c", code], timeout=300)
    return "DONE"


main(Benchmark(name="fake", results_dir=tmp / "results",
               load_tasks=lambda: [{"id": "t0"}],
               runners={"fake": Runner(name="fake", run=run_fn, environment=env_cm)},
               judge=Judge(fn=lambda *a: {"verdict": "SUCCESS", "reason": ""},
                           is_deterministic=True)),
     argv=["--system", "fake"])
"""


def test_sigterm_reaches_the_agent_grandchild_group_through_a_real_core_run_child(tmp_path):
    import signal
    import time
    (tmp_path / "run_py.py").write_text(_GRANDCHILD_RUN_PY)
    proc = _start_driver(tmp_path, "grandchild")
    gc_file = tmp_path / "grandchild.pid"
    deadline = time.time() + 30
    while not (gc_file.exists() and gc_file.read_text()):
        assert time.time() < deadline and proc.poll() is None, proc.communicate()[0]
        time.sleep(0.1)
    time.sleep(0.5)   # let _run_raw register the group
    grandchild = int(gc_file.read_text())
    assert os.getpgid(grandchild) == grandchild   # it leads its own session/group
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 128 + signal.SIGTERM, out
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.killpg(grandchild, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the agent grandchild's process group survived the driver's SIGTERM")
    with pytest.raises(ProcessLookupError):   # the run.py child was reaped by the driver
        os.kill(int((tmp_path / "child.pid").read_text()), 0)
    assert (tmp_path / "teardown_ran").exists()   # the child's environment teardown ran
    infra = json.loads((tmp_path / "results" / "fake" / "t0" / "run_1" / "infra_error.json")
                       .read_text())
    assert infra[-1]["outcome"] == "INTERRUPTED"
    log = (tmp_path / "scripts" / "g_official361_sonnet_fake.log").read_text()
    assert "0 killed" in log and "SIGKILL" not in log   # orderly exit within the grace period
    assert "run_id" in json.loads((tmp_path / "sweep.json").read_text())
