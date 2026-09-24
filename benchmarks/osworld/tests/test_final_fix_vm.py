"""Final-review fix wave, VM part (V1-V5): kvm setup failures are retryable infra errors, the
driver's systemic/auth stops, the loopback-address refusal, the driver-verified probe marker,
the qcow2 content hash and the client password on the drift list. Fake docker/children only."""
import functools
import json
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import ContainerError

from benchmarks.osworld import config
from benchmarks.osworld.env import kvm_vm
from benchmarks.osworld.tests.test_kvm_vm import _client
from benchmarks.osworld.tests.test_official361_driver import (
    _IMAGE, _batch, _start_driver, _unit)
from core.judge import Judge
from core.run import Benchmark, Runner, _main
from scripts import g_official361_driver as driver

SHA = "ab" * 32


# ---- V1: kvm setup failures raise, are recorded as retryable infra, container removed -------

@pytest.mark.parametrize("ready,configured", [("VM not ready after 300s", None),
                                              (None, "setup step 2 failed")])
def test_kvm_setup_failure_raises_and_still_removes_the_container(ready, configured):
    client, container = _client()
    with patch.object(kvm_vm, "_wait_ready", return_value=ready), \
         patch.object(kvm_vm, "_configure", return_value=configured):
        with pytest.raises(kvm_vm.KvmSetupError, match=ready or configured):
            with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client):
                pytest.fail("the runner must never be reached on a failed setup")
    container.stop.assert_called_once()
    container.remove.assert_called_once_with(v=True)


def test_kvm_setup_failure_is_a_retryable_infra_error_through_core_run(tmp_path):
    client, container = _client()
    ran = []
    runner = Runner(name="kvm", run=lambda task, **kw: ran.append(task) or "DONE",
                    environment=functools.partial(kvm_vm.kvm_environment, client=client),
                    self_eval=True)
    bench = Benchmark(name="fake", results_dir=tmp_path, load_tasks=lambda: [{"id": "t0"}],
                      runners={"kvm": runner},
                      judge=Judge(fn=lambda *a: {"verdict": "SUCCESS", "reason": ""},
                                  is_deterministic=True))
    with patch.object(kvm_vm, "_wait_ready", return_value="VM not ready after 300s"):
        _main(bench, argv=["--system", "kvm"])   # no signal handlers / .env in pytest
    out = tmp_path / "kvm" / "t0" / "run_1"
    infra = json.loads((out / "infra_error.json").read_text())[-1]
    assert infra["outcome"] == "ENV_SETUP_FAILED" and infra["error_type"] == "KvmSetupError"
    assert "not ready" in infra["error"]
    assert not (out / "eval.json").exists() and not ran
    container.remove.assert_called_once_with(v=True)


def test_env_setup_failures_count_toward_the_stuck_rule():
    unit = _unit("s", 1, fresh="ENV_SETUP_FAILED", history=["ENV_SETUP_FAILED"] * 3)
    assert driver.stuck_units([_batch(0, [unit])]) == [("s", 1)]


def test_decide_stops_when_most_attempted_units_hit_a_non_quota_infra_error():
    units = [_unit(f"t{i}", 1, fresh="ENV_SETUP_FAILED", history=["ENV_SETUP_FAILED"])
             for i in range(4)] + [_unit("ok", 1)]
    assert driver.decide([_batch(0, units)]) == "stop"   # 4/5 = 80%
    assert driver.systemic_failure([_batch(0, units)])


def test_decide_ignores_quota_and_interrupts_for_the_systemic_rule():
    units = ([_unit(f"q{i}", 1, fresh="RATE_LIMITED", history=["RATE_LIMITED"]) for i in range(3)]
             + [_unit(f"i{i}", 1, fresh="INTERRUPTED", history=["INTERRUPTED"]) for i in range(2)])
    assert not driver.systemic_failure([_batch(0, units)])
    three_of_five = [_unit(f"t{i}", 1, fresh="INFRA_FLAKE", history=["INFRA_FLAKE"])
                     for i in range(3)] + [_unit("a", 1), _unit("b", 1)]
    assert driver.decide([_batch(0, three_of_five)]) == "continue"   # 60% < 80%


@pytest.mark.parametrize("addr", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_addr_with_a_remote_docker_host_is_refused(addr):
    msg = kvm_vm.loopback_conflict(addr, "ssh://me@gpu-host")
    assert msg and "random" in msg and addr in msg
    assert kvm_vm.loopback_conflict(addr, "unix:///var/run/docker.sock") is None
    assert kvm_vm.loopback_conflict(addr, "") is None
    assert kvm_vm.loopback_conflict("10.0.0.5", "ssh://me@gpu-host") is None


def test_kvm_preflight_refuses_a_loopback_addr_with_a_remote_docker_host(monkeypatch):
    monkeypatch.setattr(config, "KVM_QCOW2_SHA256", SHA)
    monkeypatch.setattr(config, "KVM_ADDR", "127.0.0.1")
    monkeypatch.setattr(config, "KVM_DOCKER_HOST", "ssh://me@gpu-host")
    c = MagicMock()
    with pytest.raises(SystemExit, match="OSW_KVM_ADDR"):
        kvm_vm.preflight(client=c)
    assert not c.ping.called


def _env(**extra):
    return {"OSW_KVM_IMAGE": _IMAGE, **extra}


def test_driver_refuses_a_loopback_addr_with_a_remote_docker_host():
    bad = driver.env_conflicts("sonnet", _env(OSW_KVM_DOCKER_HOST="ssh://me@gpu-host"))
    assert any("OSW_KVM_ADDR" in c for c in bad)   # unset addr = config's 127.0.0.1
    assert not driver.env_conflicts("sonnet", _env(OSW_KVM_DOCKER_HOST="ssh://me@gpu-host",
                                                   OSW_KVM_ADDR="10.1.2.3"))
    assert not driver.env_conflicts("sonnet", _env())


# ---- V3: the first AUTH_ERROR stops the driver ----------------------------------------------

def test_decide_stops_on_the_first_auth_error():
    units = [_unit("a", 1, fresh="AUTH_ERROR", history=["AUTH_ERROR"])] + [
        _unit(f"t{i}", 1) for i in range(9)]
    assert driver.decide([_batch(0, units)]) == "stop"
    assert driver.auth_errors([_batch(0, units)]) == [("a", 1)]


# ---- V4: the qcow2 content hash ---------------------------------------------------------------

def _pf(runs):
    c = MagicMock()
    c.containers.run.side_effect = runs
    return c


@pytest.fixture
def sha_set(monkeypatch):
    monkeypatch.setattr(config, "KVM_QCOW2_SHA256", SHA)
    monkeypatch.setattr(config, "KVM_QCOW2", "rel/Ubuntu.qcow2")
    monkeypatch.delenv("OSW_OFFICIAL_PREFLIGHT_OK", raising=False)


def test_preflight_hashes_the_qcow2_in_a_probe_container(sha_set):
    c = _pf([b"", b"", f"{SHA}  /q\n".encode()])
    kvm_vm.preflight(client=c)
    call = c.containers.run.call_args_list[2]
    assert "sha256sum /q" in " ".join(call.kwargs["entrypoint"])
    assert call.kwargs["mounts"][0]["Target"] == "/q"


def test_preflight_fails_closed_on_a_different_qcow2(sha_set):
    other = "cd" * 32
    c = _pf([b"", b"", f"{other}  /q\n".encode()])
    with pytest.raises(SystemExit) as e:
        kvm_vm.preflight(client=c)
    assert SHA in str(e.value) and other in str(e.value)


def test_preflight_fails_closed_when_the_hash_probe_fails(sha_set):
    c = _pf([b"", b"", ContainerError("c", 1, "sha256sum", config.KVM_IMAGE, b"")])
    with pytest.raises(SystemExit, match="sha256"):
        kvm_vm.preflight(client=c)


def test_children_of_a_verified_driver_run_skip_only_the_hash(sha_set, monkeypatch):
    monkeypatch.setenv("OSW_OFFICIAL_PREFLIGHT_OK", "drv")
    monkeypatch.setenv("OSW_KVM_DRIVER_RUN", "drv")
    c = _pf([b"", b""])
    kvm_vm.preflight(client=c)
    assert c.containers.run.call_count == 2   # /dev/kvm and qcow2-file probes still run


# ---- V5: the client password is on the drift list ------------------------------------------

def test_client_password_must_stay_the_default():
    bad = driver.env_conflicts("sonnet", _env(OSW_KVM_CLIENT_PASSWORD="hunter2"))
    assert any(c.startswith("OSW_KVM_CLIENT_PASSWORD=") for c in bad)
    assert driver.child_env("sonnet", {})["OSW_KVM_CLIENT_PASSWORD"] == "password"
    assert not driver.env_conflicts("sonnet", _env(OSW_KVM_CLIENT_PASSWORD="password"))


def test_claude_code_version_must_stay_the_pin():
    bad = driver.env_conflicts("sonnet", _env(OSW_CLAUDE_CODE_VERSION="9.9.9"))
    assert any(c.startswith("OSW_CLAUDE_CODE_VERSION=") for c in bad)


# ---- V2: the driver tells its children the live probes already passed ----------------------

def test_children_receive_the_preflight_marker_equal_to_the_driver_run(tmp_path):
    from benchmarks.osworld.tests.test_official361_driver import _signal_driver_once_children_run
    proc = _start_driver(tmp_path, "signal")
    _signal_driver_once_children_run(tmp_path, proc)
    run_id = (tmp_path / "child1.pid.run").read_text()
    assert run_id and (tmp_path / "child1.pid.ok").read_text() == run_id
    # the in-process preflight itself ran without any marker (a stale one from the caller's
    # shell is dropped before it)
    assert (tmp_path / "preflight_ok.txt").read_text() == ""
