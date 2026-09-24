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
_IMAGE = "happysixd/osworld-docker@sha256:" + "0123456789abcdef" * 4   # a pinned digest


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
    no_outcome = lambda u, n: {**u, "no_outcome": True, "no_outcomes": n}
    outcome = lambda u: {**u, "no_outcome": False, "no_outcomes": 0}
    assert results == [
        _batch(0, [outcome(_unit("a", 1, "RATE_LIMITED", ["RATE_LIMITED"])),
                   no_outcome(_unit("a", 2), 1)]),
        _batch(2, [no_outcome(_unit("b", 1, None, ["INFRA_FLAKE"]), 1)]),
    ]
    assert driver.decide(results) == "stop"   # b's child failed without writing anything


# ---- --dry-run ----------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_runs_nothing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"PYTHONPATH": str(_ROOT), "ARM": "sonnet", "PARALLEL": "3", "OSW_KVM_IMAGE": _IMAGE,
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
    conflicts = driver.env_conflicts("sonnet", {"OSW_KVM_IMAGE": _IMAGE, name: value})
    assert len(conflicts) == 1 and conflicts[0].startswith(name + "=")


def test_env_conflicts_accepts_defaults_protocol_values_and_host_settings():
    environ = {"OSW_ZOOM_BATCH": "0", "OSW_PINNED_EVALUATORS": "1", "OSW_MAX_TURNS": "150",
               "OSW_TASK_TIMEOUT": "", "OSW_OBSERVATION": "screenshot+a11y",
               "OSW_PROTOCOL": "official", "OSW_BACKEND": "kvm", "OSW_MAX_STEPS": "100",
               "OSW_KVM_ADDR": "10.0.0.2", "OSW_KVM_QCOW2": "/data/Ubuntu.qcow2",
               "OSW_ASTRA_REASONING_EFFORT": "xhigh", "OSW_KVM_IMAGE": _IMAGE}
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


_DRIVER_UNDER_TEST = """
import json, os, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import scripts.g_official361_driver as d
tmp, mode = Path(sys.argv[1]), sys.argv[3]
fb = types.ModuleType("benchmarks.osworld.benchmark")
class _Runners(dict):
    def __missing__(self, k):
        return types.SimpleNamespace(preflight=lambda: None)
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

class _Containers:   # fake docker: records every list filter and removal
    def list(self, all=False, filters=None):
        if mode == "docker_error":
            raise RuntimeError("docker daemon unreachable")
        (tmp / "sweep.json").write_text(json.dumps(filters))
        run = os.environ["OSW_KVM_DRIVER_RUN"]
        return [types.SimpleNamespace(labels={"osworld.driver_run": run}, id="mine",
                                      remove=lambda **kw: (tmp / "removed.json").write_text(
                                          json.dumps(kw)))]
d._docker_client = lambda: types.SimpleNamespace(containers=_Containers())

_real = subprocess.Popen
n = [0]
def fake_popen(cmd, **kw):   # a fake run.py child: sleeps; the second one ignores SIGTERM
    n[0] += 1
    code = ("import os, signal, sys, time\\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if n[0] == 2 else "")
            + "open(sys.argv[1] + '.run', 'w').write(os.environ.get('OSW_KVM_DRIVER_RUN', ''))\\n"
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
sys.exit(d.main([]))
"""


def _start_driver(tmp_path, mode):
    helper = tmp_path / "helper.py"
    helper.write_text(_DRIVER_UNDER_TEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"ARM": "sonnet", "PARALLEL": "2", "OSW_KVM_IMAGE": _IMAGE})
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


def test_sigterm_terminates_the_children_sweeps_this_runs_containers_and_exits_nonzero(tmp_path):
    import signal
    proc = _start_driver(tmp_path, "signal")
    pid_files, out = _signal_driver_once_children_run(tmp_path, proc)
    assert proc.returncode == 128 + signal.SIGTERM, out
    for p in pid_files:
        with pytest.raises(ProcessLookupError):
            os.kill(int(p.read_text()), 0)
    # every child got the same per-driver-run id, and the sweep looked for exactly that label
    run_ids = {(tmp_path / f"child{i}.pid.run").read_text() for i in (1, 2)}
    assert len(run_ids) == 1 and len(run_ids.pop()) == 32
    run_id = (tmp_path / "child1.pid.run").read_text()
    assert json.loads((tmp_path / "sweep.json").read_text()) == {
        "label": f"osworld.driver_run={run_id}"}
    assert json.loads((tmp_path / "removed.json").read_text()) == {"force": True, "v": True}
    log = (tmp_path / "scripts" / "g_official361_sonnet.log").read_text()
    assert "SIGTERM" in log and "1 killed" in log and "removed 1 container" in log
    # every harness knob reaches the child pinned at its default, so the repo .env's setdefault
    # can no longer inject one mid-campaign (minor 4)
    assert json.loads((tmp_path / "child1.pid.env").read_text()) == {
        "OSW_ZOOM_BATCH": "0", "OSW_TASK_TIMEOUT": ""}


def test_a_unit_already_stuck_from_an_earlier_session_stops_before_any_vm_starts(tmp_path):
    proc = _start_driver(tmp_path, "prestuck")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 3, out
    assert not (tmp_path / "child1.pid").exists()
    log = (tmp_path / "scripts" / "g_official361_sonnet.log").read_text()
    assert "STOP" in log and "t0#1" in log


def test_a_docker_error_during_the_sweep_is_logged_not_raised_over_the_signal_exit(tmp_path):
    import signal
    proc = _start_driver(tmp_path, "docker_error")
    _, out = _signal_driver_once_children_run(tmp_path, proc)
    assert proc.returncode == 128 + signal.SIGTERM, out
    assert "Traceback" not in out
    log = (tmp_path / "scripts" / "g_official361_sonnet.log").read_text()
    assert "container sweep failed" in log and "docker daemon unreachable" in log


def test_the_sweep_also_runs_on_normal_exit(tmp_path):
    proc = _start_driver(tmp_path, "done")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert json.loads((tmp_path / "sweep.json").read_text())["label"].startswith(
        "osworld.driver_run=")
    assert "removed 1 container" in (tmp_path / "scripts" / "g_official361_sonnet.log").read_text()


# ---- the container sweep (fix round 0b) ----------------------------------------------------

class _FakeContainer:
    def __init__(self, labels):
        self.labels, self.id, self.removed = labels, str(labels), None

    def remove(self, **kw):
        self.removed = kw


def test_sweep_removes_only_containers_labelled_with_this_driver_run():
    from types import SimpleNamespace as NS
    mine = [_FakeContainer({"osworld.driver_run": "r1", "osworld.task": "t"}),
            _FakeContainer({"osworld.driver_run": "r1"})]
    others = [_FakeContainer({"osworld.driver_run": "r2"}), _FakeContainer({}),
              _FakeContainer({"osworld.driver_run": ""}), _FakeContainer(None)]
    seen = {}

    def list_(all=False, filters=None):   # a sloppy daemon/filter: returns everything
        seen.update(all=all, filters=filters)
        return mine + others
    lines = []
    removed = driver.sweep_containers("r1", lines.append,
                                      client=NS(containers=NS(list=list_)))
    assert removed == 2
    assert seen == {"all": True, "filters": {"label": "osworld.driver_run=r1"}}
    assert all(c.removed == {"force": True, "v": True} for c in mine)
    assert all(c.removed is None for c in others)
    assert lines == ["container sweep: removed 2 container(s) labelled osworld.driver_run=r1"]


def test_sweep_logs_docker_errors_instead_of_raising():
    from types import SimpleNamespace as NS

    class Broken(_FakeContainer):
        def remove(self, **kw):
            raise RuntimeError("conflict")
    ok, broken = _FakeContainer({"osworld.driver_run": "r1"}), Broken({"osworld.driver_run": "r1"})
    lines = []
    assert driver.sweep_containers(
        "r1", lines.append, client=NS(containers=NS(list=lambda **kw: [broken, ok]))) == 1
    assert ok.removed and any("conflict" in l for l in lines)

    def down(**kw):
        raise RuntimeError("daemon down")
    lines = []
    assert driver.sweep_containers("r1", lines.append,
                                   client=NS(containers=NS(list=down))) == 0
    assert any("container sweep failed" in l and "daemon down" in l for l in lines)


def test_sweep_refuses_an_empty_run_id():
    # an empty id would match every container started outside a driver (label value "")
    with pytest.raises(ValueError):
        driver.sweep_containers("", print, client=None)


def test_driver_run_id_is_not_on_the_refuse_list():
    environ = {"OSW_KVM_IMAGE": _IMAGE, "OSW_KVM_DRIVER_RUN": "abc"}
    assert driver.env_conflicts("sonnet", environ) == []


# ---- fix round 1 ----------------------------------------------------------------------------

@pytest.mark.parametrize("image", [None, "", "happysixd/osworld-docker",
                                   "happysixd/osworld-docker:latest",
                                   "other/osworld-docker@sha256:" + "0" * 64,
                                   "happysixd/osworld-docker@sha256:" + "0" * 63,
                                   "happysixd/osworld-docker@sha256:" + "A" * 64])
def test_the_kvm_image_must_be_a_pinned_digest_of_the_official_image(image):
    environ = {} if image is None else {"OSW_KVM_IMAGE": image}
    conflicts = driver.env_conflicts("sonnet", environ)
    assert len(conflicts) == 1 and conflicts[0].startswith("OSW_KVM_IMAGE=")
    assert driver.env_conflicts("sonnet", {"OSW_KVM_IMAGE": _IMAGE}) == []


def test_child_env_pins_every_harness_knob_at_its_default():
    env = driver.child_env("sonnet", {})
    assert env == {**driver._HARNESS_KNOB_DEFAULTS, **driver.protocol_env("sonnet", {})}
    assert env["OSW_TASK_TIMEOUT"] == "" and env["OSW_ZOOM_BATCH"] == "0"


def test_stuck_pending_finds_units_stuck_in_an_earlier_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    for (tid, k), hist in {("a", 1): ["HARNESS_ERROR", "INFRA_FLAKE", "HARNESS_ERROR"],
                           ("b", 1): ["RATE_LIMITED"] * 4 + ["HARNESS_ERROR"] * 2}.items():
        out = tmp_path / "sys" / tid / f"run_{k}"
        out.mkdir(parents=True)
        (out / "infra_error.json").write_text(json.dumps([{"outcome": o} for o in hist]))
    assert driver.stuck_pending("sys", [("a", 1), ("b", 1), ("c", 1)]) == [("a", 1)]


def test_a_unit_that_ends_with_no_outcome_counts_toward_the_stuck_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(driver.subprocess, "Popen", _FakePopen)
    _FakePopen.launched = []
    # a#1 scored; a#2 wrote nothing; a#3 left an unparsable infra_error.json
    _FakePopen.script = {"a": (0, {("a", 1): "EVAL", ("a", 3): "GARBAGE"})}
    no_outcomes = {("a", 2): 2}   # two earlier rounds of this session already ended that way
    results = driver.run_round("sys", [["a"]], [("a", 1), ("a", 2), ("a", 3)], runs=5,
                               log_file=None, no_outcomes=no_outcomes)
    units = {(u["task_id"], u["run_idx"]): u for u in results[0]["units"]}
    assert not units[("a", 1)]["no_outcome"]
    assert units[("a", 2)]["no_outcome"] and units[("a", 2)]["no_outcomes"] == 3
    assert units[("a", 3)]["no_outcome"] and units[("a", 3)]["no_outcomes"] == 1
    assert no_outcomes == {("a", 2): 3, ("a", 3): 1}
    assert driver.stuck_units(results) == [("a", 2)]
    assert driver.decide(results) == "stop"
    # rate limits never count, a no-outcome does
    rl = _unit("q", 1, fresh=None, history=["HARNESS_ERROR", "RATE_LIMITED", "HARNESS_ERROR"])
    assert driver.stuck_units([_batch(0, [{**rl, "no_outcomes": 1}])]) == [("q", 1)]
    assert driver.stuck_units([_batch(0, [rl])]) == []
