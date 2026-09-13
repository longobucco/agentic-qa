"""Pure regression tests for env.sandbox's open-book provisioning path -- no live sandbox, no
mitmproxy. Covers the host-proxy routing decision added after confirming live that
SetupController._download_setup runs requests.get() on the HARNESS HOST, not in the guest (see
astra_openbook_campaign_lock.json's known_issues.non_chrome_egress_not_proxied)."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from benchmarks.osworld import config
from benchmarks.osworld.env import sandbox


def _fake_ctrl():
    return MagicMock(base_url="http://fake-controller")


def _ready_check():
    return {"ready": True, "reason": None,
           "bundle_dir": tempfile.mkdtemp(), "manifest_sha256": "deadbeef"}


def test_a_task_without_download_never_touches_the_host_proxy():
    task = {"id": "t-no-download", "config": []}
    ctrl = _fake_ctrl()
    with patch("benchmarks.osworld.open_book_preflight.task_check", return_value=_ready_check()), \
         patch("benchmarks.osworld.open_book_preflight.proxy_tags_for", return_value=set()), \
         patch("benchmarks.osworld.env.guest_proxy.start"), \
         patch("benchmarks.osworld.env.sandbox.provision", return_value=(MagicMock(), ctrl)), \
         patch("benchmarks.osworld.env.sandbox._run_config", return_value=None) as run_config, \
         patch("benchmarks.osworld.env.host_proxy.host_proxy") as host_proxy_cm:
        with patch.object(config, "OPENBOOK_IMAGE", "fake-image"):
            result_ctrl, err = sandbox._provision_and_configure_openbook(task, {})
    assert err is None
    assert run_config.called
    assert not host_proxy_cm.called


def test_a_task_tagged_host_side_config_download_routes_through_the_host_proxy():
    task = {"id": "t-download", "config": [{"type": "download", "parameters": {}}]}
    ctrl = _fake_ctrl()
    calls = []
    with patch("benchmarks.osworld.open_book_preflight.task_check", return_value=_ready_check()), \
         patch("benchmarks.osworld.open_book_preflight.proxy_tags_for",
              return_value={"host_side_config_download"}), \
         patch("benchmarks.osworld.env.guest_proxy.start"), \
         patch("benchmarks.osworld.env.sandbox.provision", return_value=(MagicMock(), ctrl)), \
         patch("benchmarks.osworld.env.sandbox._run_config",
              side_effect=lambda *a, **k: calls.append("run_config") or None), \
         patch("benchmarks.osworld.env.host_proxy.host_proxy") as host_proxy_cm, \
         patch("benchmarks.osworld.env.host_proxy.scoped_env") as scoped_env_cm:
        host_proxy_cm.return_value.__enter__.return_value = {"proxy_url": "http://x", "port": 1}
        scoped_env_cm.return_value.__enter__.side_effect = lambda: calls.append("scoped_env")
        with patch.object(config, "OPENBOOK_IMAGE", "fake-image"):
            result_ctrl, err = sandbox._provision_and_configure_openbook(task, {})
    assert err is None
    assert host_proxy_cm.called, "a download-tagged task must start the host proxy"
    assert calls == ["scoped_env", "run_config"], "config must run INSIDE the scoped host-proxy env"
    no_proxy_hosts = scoped_env_cm.call_args.kwargs.get("no_proxy_hosts", ())
    assert "127.0.0.1" in no_proxy_hosts and "localhost" in no_proxy_hosts and \
        "fake-controller" in no_proxy_hosts, (
        "_download_setup also uploads back to the real controller in the same requests "
        "session -- confirmed live that a blanket HTTP_PROXY hijacks that upload (502 from our "
        "own fixture proxy) unless the controller's host (and loopback, for other call paths) "
        "is excluded")


def test_a_host_proxy_startup_failure_becomes_an_environment_error_not_a_crash():
    task = {"id": "t-download", "config": [{"type": "download", "parameters": {}}]}
    ctrl = _fake_ctrl()
    with patch("benchmarks.osworld.open_book_preflight.task_check", return_value=_ready_check()), \
         patch("benchmarks.osworld.open_book_preflight.proxy_tags_for",
              return_value={"host_side_config_download"}), \
         patch("benchmarks.osworld.env.guest_proxy.start"), \
         patch("benchmarks.osworld.env.sandbox.provision", return_value=(MagicMock(), ctrl)), \
         patch("benchmarks.osworld.env.host_proxy.host_proxy",
              side_effect=__import__(
                  "benchmarks.osworld.env.host_proxy", fromlist=["HostProxyError"]
              ).HostProxyError("mitmdump not found")):
        with patch.object(config, "OPENBOOK_IMAGE", "fake-image"):
            result_ctrl, err = sandbox._provision_and_configure_openbook(task, {})
    assert err is not None and "host proxy" in err


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
