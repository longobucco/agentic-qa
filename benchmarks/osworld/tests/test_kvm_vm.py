"""kvm backend: the official OSWorld VM in upstream's Docker-provider container, with the
published (random) host ports used by every consumer. All against a fake docker client."""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from benchmarks.osworld import config
from benchmarks.osworld.env import kvm_vm

PORTS = {"5000/tcp": [{"HostPort": "32801"}], "9222/tcp": [{"HostPort": "32802"}],
         "8080/tcp": [{"HostPort": "32803"}], "8006/tcp": [{"HostPort": "32804"}]}


def _client():
    c = MagicMock()
    container = MagicMock(id="abc123", attrs={"NetworkSettings": {"Ports": PORTS}})
    c.containers.run.return_value = container
    return c, container


def test_published_ports():
    _, container = _client()
    assert kvm_vm.published_ports(container) == {5000: 32801, 9222: 32802, 8080: 32803,
                                                 8006: 32804}


def test_container_matches_upstream_docker_provider(monkeypatch):
    client, container = _client()
    with patch.object(kvm_vm, "_wait_ready", return_value=None), \
         patch.object(kvm_vm, "_configure", return_value=None):
        with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client) as env:
            ctrl = env.browser
    kw = client.containers.run.call_args.kwargs
    assert client.containers.run.call_args.args[0] == config.KVM_IMAGE
    assert kw["environment"] == {"DISK_SIZE": "32G", "RAM_SIZE": "4G", "CPU_CORES": "4"}
    assert kw["devices"] == ["/dev/kvm"] and kw["cap_add"] == ["NET_ADMIN"]
    assert kw["volumes"] == {config.KVM_QCOW2: {"bind": "/System.qcow2", "mode": "ro"}}
    assert set(kw["ports"]) == {5000, 9222, 8080, 8006}
    assert ctrl.base_url == f"http://{config.KVM_ADDR}:32801"
    assert (ctrl.chromium_port, ctrl.vlc_port) == (32802, 32803)
    assert ctrl.client_password == config.KVM_CLIENT_PASSWORD
    container.stop.assert_called_once()
    container.remove.assert_called_once_with(v=True)


def test_container_is_removed_even_when_the_body_raises():
    client, container = _client()
    with patch.object(kvm_vm, "_wait_ready", return_value=None), \
         patch.object(kvm_vm, "_configure", return_value=None):
        with pytest.raises(KeyboardInterrupt):
            with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client):
                raise KeyboardInterrupt
    container.remove.assert_called_once_with(v=True)


def test_container_is_removed_even_when_stop_raises():
    client, container = _client()
    container.stop.side_effect = RuntimeError("daemon hiccup")
    with patch.object(kvm_vm, "_wait_ready", return_value=None), \
         patch.object(kvm_vm, "_configure", return_value=None):
        with pytest.raises(RuntimeError):
            with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client):
                pass
    container.remove.assert_called_once_with(v=True)


def test_container_is_removed_when_setup_raises():
    client, container = _client()
    with patch.object(kvm_vm, "_wait_ready", return_value=None), \
         patch.object(kvm_vm, "_configure", side_effect=RuntimeError("setup blew up")):
        with pytest.raises(RuntimeError):
            with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client):
                pass
    container.stop.assert_called_once()
    container.remove.assert_called_once_with(v=True)


def test_vm_never_ready_is_a_setup_error_not_a_crash():
    client, container = _client()
    with patch.object(kvm_vm, "_wait_ready", return_value="VM not ready after 300s"):
        with kvm_vm.kvm_environment({"id": "t", "config": []}, client=client) as env:
            assert "not ready" in env.setup_error
    container.remove.assert_called_once_with(v=True)


def test_configure_runs_setup_with_upstream_retries_and_no_cdp_forwarder():
    with patch("benchmarks.osworld.env.sandbox._run_config", return_value=None) as rc:
        kvm_vm._configure("CTRL", {"id": "t"})
    assert rc.call_args.kwargs == {"enable_cdp_forwarder": False, "setup_attempts": 5,
                                   "verify_launches": False}


def test_setup_controller_gets_mapped_ports_and_password():
    from benchmarks.osworld.env import osworld_eval
    sc = osworld_eval.make_setup_controller("http://10.0.0.5:32801", chromium_port=32802,
                                            vlc_port=32803, client_password="password")
    assert (sc.chromium_port, sc.vlc_port, sc.client_password) == (32802, 32803, "password")
    assert sc.http_server == "http://10.0.0.5:32801"
    assert sc.vm_ip == "10.0.0.5"


def test_setup_controller_defaults_unchanged():
    from benchmarks.osworld.env import osworld_eval
    sc = osworld_eval.make_setup_controller("http://127.0.0.1:5000")
    assert (sc.chromium_port, sc.vlc_port, sc.client_password) == (9222, 8080, "")


def test_run_config_retries_a_false_setup(monkeypatch):
    from benchmarks.osworld.env import sandbox
    sc = MagicMock()
    sc.setup.side_effect = [False, False, True]
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller", return_value=sc), \
         patch.object(sandbox.time, "sleep"):
        err = sandbox._run_config(MagicMock(base_url="http://x:1"), {"id": "t", "config": [
            {"type": "sleep", "parameters": {"seconds": 0}}]}, setup_attempts=5,
            verify_launches=False)
    assert err is None and sc.setup.call_count == 3


def test_run_config_all_false_is_a_setup_error():
    from benchmarks.osworld.env import sandbox
    sc = MagicMock()
    sc.setup.return_value = False
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller", return_value=sc), \
         patch.object(sandbox.time, "sleep"):
        err = sandbox._run_config(MagicMock(base_url="http://x:1"), {"id": "t", "config": [
            {"type": "sleep", "parameters": {"seconds": 0}}]}, setup_attempts=5,
            verify_launches=False)
    assert err == "config setup failed: SetupController.setup returned False"
    assert sc.setup.call_count == 5


def test_run_config_defaults_keep_one_attempt_and_launch_verification():
    from benchmarks.osworld.env import sandbox
    sc = MagicMock()
    sc.setup.return_value = None     # today's upstream returns None on the happy path
    ctrl = NS(base_url="http://x:1")  # a Daytona controller: no mapped-port attributes
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=sc) as msc, \
         patch.object(sandbox, "_verify_launches", return_value="VERIFIED") as vl:
        err = sandbox._run_config(ctrl, {"id": "t", "config": [
            {"type": "sleep", "parameters": {"seconds": 0}}]})
    assert err == "VERIFIED" and vl.called and sc.setup.call_count == 1
    assert msc.call_args.kwargs["chromium_port"] is None
    assert msc.call_args.kwargs["vlc_port"] is None
    assert msc.call_args.kwargs["client_password"] == ""


def test_run_config_forwards_the_mapped_ports():
    from benchmarks.osworld.env import sandbox
    sc = MagicMock()
    ctrl = NS(base_url="http://10.0.0.5:32801", chromium_port=32802, vlc_port=32803,
              client_password="password")
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller",
               return_value=sc) as msc:
        sandbox._run_config(ctrl, {"id": "t", "config": [
            {"type": "sleep", "parameters": {"seconds": 0}}]}, verify_launches=False)
    assert msc.call_args.args[0] == "http://10.0.0.5:32801"
    kw = msc.call_args.kwargs
    assert (kw["chromium_port"], kw["vlc_port"], kw["client_password"]) == \
        (32802, 32803, "password")


def test_env_adapter_setup_controller_forwards_the_mapped_ports():
    from benchmarks.osworld.env import osworld_eval
    with patch.object(osworld_eval, "make_setup_controller") as msc:
        env = osworld_eval._EnvAdapter(MagicMock(), "http://10.0.0.5:32801", [],
                                       cache_dir="/tmp/x", chromium_port=32802, vlc_port=32803,
                                       client_password="password")
        env.setup_controller
    assert (env.chromium_port, env.vlc_port) == (32802, 32803)
    msc.assert_called_once_with("http://10.0.0.5:32801", cache_dir="/tmp/x",
                                chromium_port=32802, vlc_port=32803,
                                client_password="password")


def test_evaluate_official_with_mapped_ports_addresses_the_vm_directly():
    """Getters build "http://{env.vm_ip}:{env.chromium_port}" for CDP and the same for VLC, so
    on kvm vm_ip must be KVM_ADDR (the controller host), not the loopback forwarder."""
    from benchmarks.osworld.env import osworld_eval
    seen = {}

    def fake_score(env, ev, func, url, cache_dir, getters, metrics, use_proxy=False,
                   cdp_forwarder=None, **kw):
        seen.update(vm_ip=env.vm_ip, server_port=env.server_port, chromium=env.chromium_port,
                    vlc=env.vlc_port, pw=env.client_password, fwd=cdp_forwarder, kw=kw)
        return 1.0

    with patch.object(osworld_eval, "_score", side_effect=fake_score), \
         patch.object(osworld_eval, "CdpForwarder") as fwd:
        r = osworld_eval.evaluate_official(
            "http://10.0.0.5:32801", {"evaluator": {"func": "x"}}, [],
            enable_cdp_forwarder=False, chromium_port=32802, vlc_port=32803,
            client_password="password")
    assert r == 1.0 and not fwd.called
    assert (seen["vm_ip"], seen["server_port"]) == ("10.0.0.5", 32801)
    assert (seen["chromium"], seen["vlc"], seen["pw"]) == (32802, 32803, "password")
    assert seen["kw"] == {"chromium_port": 32802, "vlc_port": 32803,
                          "client_password": "password"}


def test_score_postconfig_uses_the_mapped_ports():
    from benchmarks.osworld.env import osworld_eval
    with patch.object(osworld_eval, "make_setup_controller") as msc:
        osworld_eval._score(MagicMock(action_history=[]),
                            {"func": "infeasible", "postconfig": [{"type": "sleep"}]},
                            "infeasible", "http://10.0.0.5:32801", "/tmp/x", None, None,
                            chromium_port=32802, vlc_port=32803, client_password="password")
    msc.assert_called_once_with("http://10.0.0.5:32801", cache_dir="/tmp/x",
                                chromium_port=32802, vlc_port=32803,
                                client_password="password")


@pytest.mark.parametrize("backend,cdp", [("daytona", True), ("kvm", False)])
def test_runner_score_threads_mapped_ports_and_cdp_choice(monkeypatch, backend, cdp):
    from benchmarks.osworld.runners import common
    monkeypatch.setattr(config, "BACKEND", backend)
    if backend == "kvm":
        ctrl = NS(base_url="http://10.0.0.5:32801", chromium_port=32802, vlc_port=32803,
                  client_password="password")
        want = (32802, 32803, "password")
    else:
        ctrl = NS(base_url="https://5000-sb.proxy.daytona.works")
        want = (None, None, "")
    with patch.object(common.osworld_eval, "evaluate_official", return_value=1.0) as ev, \
         patch.object(common.osworld_eval, "hash_gold_artifacts", return_value={}):
        rec = common._score(ctrl, {"id": "t", "evaluator": {"func": "x"}}, "done", None)
    assert rec["verdict"] == "SUCCESS"
    kw = ev.call_args.kwargs
    assert ev.call_args.args[0] == ctrl.base_url
    assert kw["enable_cdp_forwarder"] is cdp
    assert (kw["chromium_port"], kw["vlc_port"], kw["client_password"]) == want


def test_run_config_default_single_attempt_ignores_false_as_before():
    from benchmarks.osworld.env import sandbox
    sc = MagicMock()
    sc.setup.return_value = False
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch("benchmarks.osworld.env.osworld_eval.make_setup_controller", return_value=sc), \
         patch.object(sandbox, "_verify_launches", return_value=None) as vl:
        err = sandbox._run_config(NS(base_url="http://x:1"), {"id": "t", "config": [
            {"type": "sleep", "parameters": {"seconds": 0}}]})
    assert err is None and vl.called and sc.setup.call_count == 1
