"""Offline tests for the kvm backend of the campaign driver (benchmarks/osworld/campaign_kvm.py)
and its wiring into the core (benchmarks/osworld/campaign.py) and the CLI
(scripts/g_official361_driver.py): the container sweep, the pinned kvm image, the loopback rule,
the kvm half of the drift checks, the kvm log name and container label, and a real kvm-backed
driver process under SIGTERM. No run.py, claude, codex or docker is ever invoked -- the docker
client and the run.py children are fakes."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.osworld import campaign, campaign_kvm as kb
from benchmarks.osworld.tests.test_campaign import _batch, _sleeper, _unit  # noqa: F401 (re-export)

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE = "happysixd/osworld-docker@sha256:" + "0123456789abcdef" * 4   # a pinned digest


# ---- the backend in the CLI and the core ----------------------------------------------------

def test_the_cli_offers_both_backends_on_the_vm_branch():
    from scripts import g_official361_driver as cli
    assert set(cli.BACKENDS) == {"daytona", "kvm"}


def test_kvm_backend_keeps_the_legacy_log_name_and_client_password_knob():
    from benchmarks.osworld import campaign, campaign_kvm as kb
    assert kb.LOG_SUFFIX == "" and kb.protocol_env() == {"OSW_BACKEND": "kvm"}
    env = campaign.child_env("sonnet", {"OSW_KVM_IMAGE": "happysixd/osworld-docker@sha256:"
                                         + "0" * 64}, kb)
    assert env["OSW_KVM_CLIENT_PASSWORD"] == "password"


def test_kvm_containers_are_labelled_with_the_generic_driver_run(monkeypatch):
    # The same fake docker client the existing kvm_vm tests use (see test_kvm_vm.py's label
    # test, which this mirrors): with OSW_DRIVER_RUN=drv the container label is drv, and
    # OSW_KVM_DRIVER_RUN alone no longer sets it.
    from unittest.mock import patch
    from benchmarks.osworld.env import kvm_vm
    from benchmarks.osworld.tests.test_kvm_vm import _client

    def label_with(name):
        monkeypatch.delenv("OSW_DRIVER_RUN", raising=False)
        monkeypatch.delenv("OSW_KVM_DRIVER_RUN", raising=False)
        monkeypatch.setenv(name, "drv")
        client, _ = _client()
        with patch.object(kvm_vm, "_wait_ready", return_value=None), \
             patch.object(kvm_vm, "_configure", return_value=None):
            with kvm_vm.kvm_environment({"id": "task-9", "config": []}, client=client):
                pass
        return client.containers.run.call_args.kwargs["labels"]["osworld.driver_run"]

    assert label_with("OSW_DRIVER_RUN") == "drv"
    assert label_with("OSW_KVM_DRIVER_RUN") == ""


def test_the_cli_hands_the_kvm_backend_to_the_core(monkeypatch):
    from scripts import g_official361_driver as cli
    monkeypatch.setenv("BACKEND", "kvm")
    seen = []
    monkeypatch.setattr(campaign, "main", lambda backend, argv: seen.append(backend.NAME) or 0)
    assert cli.main(["--dry-run"]) == 0 and seen == ["kvm"]


def test_importing_the_kvm_backend_and_cli_does_not_import_config():
    # config reads the environment at import time, so the driver must export the protocol
    # environment BEFORE anything imports it (an earlier driver in this project got this wrong).
    code = ("import sys; import benchmarks.osworld.campaign_kvm, scripts.g_official361_driver; "
            "print('benchmarks.osworld.config' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(_ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


# ---- protocol environment (the kvm half) ----------------------------------------------------

_COMMON = {"OSW_PROTOCOL": "official", "OSW_BACKEND": "kvm", "OSW_POPULATION": "verified361",
           "OSW_MAX_STEPS": "100", "OSW_SCREEN_WIDTH": "1920", "OSW_SCREEN_HEIGHT": "1080"}


def test_protocol_env_for_sonnet():
    assert campaign.protocol_env("sonnet", {}, kb) == {
        **_COMMON, "OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
        "OSW_MAX_OUTPUT_TOKENS": "128000", "OSW_SYSTEM_SUFFIX": "protocol361"}


def test_protocol_env_for_astra_requires_the_callers_effort():
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        campaign.protocol_env("astra", {}, kb)
    with pytest.raises(SystemExit, match="OSW_ASTRA_REASONING_EFFORT"):
        campaign.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "  "}, kb)
    assert campaign.protocol_env("astra", {"OSW_ASTRA_REASONING_EFFORT": "xhigh"}, kb) == {
        **_COMMON, "OSW_ASTRA_MODEL": "gpt-6-astra", "OSW_ASTRA_REASONING_EFFORT": "xhigh",
        "OSW_ASTRA_CAMPAIGN_LOCK": "astra_official361_lock.json",
        "OSW_ASTRA_SYSTEM_SUFFIX": "protocol361"}


def test_child_env_pins_every_harness_knob_at_its_default():
    env = campaign.child_env("sonnet", {}, kb)
    assert env == {**campaign._HARNESS_KNOB_DEFAULTS, **kb.harness_knob_defaults(),
                   **campaign.protocol_env("sonnet", {}, kb)}
    assert env["OSW_TASK_TIMEOUT"] == "" and env["OSW_ZOOM_BATCH"] == "0"
    assert env["OSW_KVM_CLIENT_PASSWORD"] == "password"


# ---- --dry-run through the CLI --------------------------------------------------------------

def test_dry_run_prints_the_plan_and_runs_nothing(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"PYTHONPATH": str(_ROOT), "BACKEND": "kvm", "ARM": "sonnet", "PARALLEL": "3",
                "OSW_KVM_IMAGE": _IMAGE, "PATH": f"{fake_bin}:{env.get('PATH', '')}"})
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


# ---- refusal of harness-altering knobs (the kvm half) ---------------------------------------

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
    conflicts = campaign.env_conflicts("sonnet", {"OSW_KVM_IMAGE": _IMAGE, name: value}, kb)
    assert len(conflicts) == 1 and conflicts[0].startswith(name + "=")


def test_env_conflicts_accepts_defaults_protocol_values_and_host_settings():
    environ = {"OSW_ZOOM_BATCH": "0", "OSW_PINNED_EVALUATORS": "1", "OSW_MAX_TURNS": "150",
               "OSW_TASK_TIMEOUT": "", "OSW_OBSERVATION": "screenshot+a11y",
               "OSW_PROTOCOL": "official", "OSW_BACKEND": "kvm", "OSW_MAX_STEPS": "100",
               "OSW_KVM_ADDR": "10.0.0.2", "OSW_KVM_QCOW2": "/data/Ubuntu.qcow2",
               "OSW_ASTRA_REASONING_EFFORT": "xhigh", "OSW_KVM_IMAGE": _IMAGE}
    assert campaign.env_conflicts("sonnet", environ, kb) == []
    assert campaign.env_conflicts("astra", environ, kb) == []
    assert campaign.env_conflicts("astra", {**environ, "OSW_ASTRA_CAMPAIGN_LOCK": "a.json"},
                                  kb) == [
        "OSW_ASTRA_CAMPAIGN_LOCK='a.json' (the protocol requires "
        "'astra_official361_lock.json')"]


def test_driver_run_id_is_not_on_the_refuse_list():
    environ = {"OSW_KVM_IMAGE": _IMAGE, "OSW_DRIVER_RUN": "abc"}
    assert campaign.env_conflicts("sonnet", environ, kb) == []


@pytest.mark.parametrize("image", [None, "", "happysixd/osworld-docker",
                                   "happysixd/osworld-docker:latest",
                                   "other/osworld-docker@sha256:" + "0" * 64,
                                   "happysixd/osworld-docker@sha256:" + "0" * 63,
                                   "happysixd/osworld-docker@sha256:" + "A" * 64])
def test_the_kvm_image_must_be_a_pinned_digest_of_the_official_image(image):
    environ = {} if image is None else {"OSW_KVM_IMAGE": image}
    conflicts = campaign.env_conflicts("sonnet", environ, kb)
    assert len(conflicts) == 1 and conflicts[0].startswith("OSW_KVM_IMAGE=")
    assert campaign.env_conflicts("sonnet", {"OSW_KVM_IMAGE": _IMAGE}, kb) == []


def test_driver_refuses_a_loopback_addr_with_a_remote_docker_host():
    env = lambda **extra: {"OSW_KVM_IMAGE": _IMAGE, **extra}
    bad = campaign.env_conflicts("sonnet", env(OSW_KVM_DOCKER_HOST="ssh://me@gpu-host"), kb)
    assert any("OSW_KVM_ADDR" in c for c in bad)   # unset addr = config's 127.0.0.1
    assert not campaign.env_conflicts("sonnet", env(OSW_KVM_DOCKER_HOST="ssh://me@gpu-host",
                                                    OSW_KVM_ADDR="10.1.2.3"), kb)
    assert not campaign.env_conflicts("sonnet", env(), kb)


# ---- the grace period (kvm teardown) --------------------------------------------------------

def test_child_grace_covers_an_orderly_kvm_teardown_and_is_what_terminate_children_uses(
        monkeypatch):
    assert campaign.CHILD_GRACE_S == 180
    monkeypatch.setattr(campaign, "CHILD_GRACE_S", 1)
    stubborn = _sleeper(ignore_term=True)
    lines = []
    assert campaign.terminate_children([stubborn], log=lines.append) == (1, 1)
    assert stubborn.poll() == -9
    assert any("SIGKILL" in l and "the backend sweep is the backstop" in l for l in lines)


# ---- a real kvm-backed driver process (fake docker, fake run.py children) -------------------

_KVM_CAMPAIGN_UNDER_TEST = """
import json, os, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import benchmarks.osworld.campaign as d
import benchmarks.osworld.campaign_kvm as kb
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
        run = os.environ["OSW_DRIVER_RUN"]
        return [types.SimpleNamespace(labels={"osworld.driver_run": run}, id="mine",
                                      remove=lambda **kw: (tmp / "removed.json").write_text(
                                          json.dumps(kw)))]
kb._docker_client = lambda: types.SimpleNamespace(containers=_Containers())

_real = subprocess.Popen
n = [0]
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
            + "{k: os.environ.get(k) for k in ('OSW_ZOOM_BATCH', 'OSW_TASK_TIMEOUT', "
            + "'OSW_KVM_CLIENT_PASSWORD')}))\\n"
            + "open(sys.argv[1], 'w').write(str(os.getpid()))\\ntime.sleep(120)\\n")
    (tmp / "dotenv").write_text("OSW_ZOOM_BATCH=1\\nOSW_TASK_TIMEOUT=60\\n"
                                "OSW_KVM_CLIENT_PASSWORD=hunter2\\n")
    return _real([sys.executable, "-c", code, str(tmp / f"child{n[0]}.pid"), sys.argv[2],
                  str(tmp / "dotenv")])
d.subprocess.Popen = fake_popen
sys.exit(d.main(kb, []))
"""


def _start_driver(tmp_path, mode):
    helper = tmp_path / "helper.py"
    helper.write_text(_KVM_CAMPAIGN_UNDER_TEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    # a stale probe marker from the caller's shell must never reach the preflight (V2)
    env.update({"ARM": "sonnet", "PARALLEL": "2", "OSW_KVM_IMAGE": _IMAGE,
                "OSW_OFFICIAL_PREFLIGHT_OK": "stale"})
    return subprocess.Popen([sys.executable, str(helper), str(tmp_path), str(_ROOT), mode],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _signal_driver_once_children_run(tmp_path, proc):
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
    # every harness knob -- the kvm backend's own client password included -- reaches the child
    # pinned at its default, so the repo .env's setdefault can no longer inject one mid-campaign
    assert json.loads((tmp_path / "child1.pid.env").read_text()) == {
        "OSW_ZOOM_BATCH": "0", "OSW_TASK_TIMEOUT": "", "OSW_KVM_CLIENT_PASSWORD": "password"}


def test_a_docker_error_during_the_sweep_is_logged_not_raised_over_the_signal_exit(tmp_path):
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


# ---- the container sweep --------------------------------------------------------------------

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
    removed = kb.sweep("r1", lines.append, client=NS(containers=NS(list=list_)))
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
    assert kb.sweep(
        "r1", lines.append, client=NS(containers=NS(list=lambda **kw: [broken, ok]))) == 1
    assert ok.removed and any("conflict" in l for l in lines)

    def down(**kw):
        raise RuntimeError("daemon down")
    lines = []
    assert kb.sweep("r1", lines.append, client=NS(containers=NS(list=down))) == 0
    assert any("container sweep failed" in l and "daemon down" in l for l in lines)


def test_sweep_refuses_an_empty_run_id():
    # an empty id would match every container started outside a driver (label value "")
    with pytest.raises(ValueError):
        kb.sweep("", print, client=None)


def test_the_sweep_label_is_the_one_kvm_vm_puts_on_its_containers():
    from benchmarks.osworld.env import kvm_vm
    assert kb.DRIVER_RUN_LABEL == kvm_vm.DRIVER_RUN_LABEL == "osworld.driver_run"
