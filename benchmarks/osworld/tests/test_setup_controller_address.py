"""The SetupController must address the guest controller at a URL that is actually true.

Most setup steps use `http_server` (overwritten with the real controller URL), but
`_update_browse_history_setup` builds its own `PythonController(self.vm_ip, self.server_port)`,
i.e. `http://{vm_ip}:{server_port}`. With a Daytona controller URL
(`https://5000-<id>.daytonaproxy01.eu`) that was `http://<proxy host>:443`, or after the CDP
forwarder's rebinding `http://127.0.0.1:443`: nothing answers, `execute_python_command` returns
None, and the step raises "'NoneType' object is not subscriptable". Found live 2026-09-29 on
44ee5668 (5/5 Sonnet runs ENVIRONMENT_ERROR). The fix hands every SetupController the same
loopback forwarder address the scoring getters already use (env/http_forwarder.py)."""
from unittest.mock import MagicMock, patch

from benchmarks.osworld.env import osworld_eval, sandbox

DAYTONA_URL = "https://5000-abc.daytonaproxy01.eu"


class _FakeLoopback:
    """Stands in for http_forwarder.LoopbackForwarder: records its target and whether it is up."""
    instances = []

    def __init__(self, target_url, **_kw):
        self.target_url, self.host, self.port, self.up = target_url, "127.0.0.1", 4242, False
        _FakeLoopback.instances.append(self)

    def start(self):
        self.up = True
        return self

    def stop(self):
        self.up = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False


def test_make_setup_controller_uses_the_given_server_address():
    sc = osworld_eval.make_setup_controller(DAYTONA_URL, server_address=("127.0.0.1", 4242))
    assert (sc.vm_ip, sc.server_port) == ("127.0.0.1", 4242)
    assert sc.http_server == DAYTONA_URL   # the routes that already worked are unchanged


def test_task_setup_reaches_the_controller_through_the_loopback_forwarder():
    """At the moment the steps run, http://{vm_ip}:{server_port} is the loopback forwarder, which
    is up and forwards to the real controller URL; it is stopped afterwards."""
    _FakeLoopback.instances = []
    seen = {}

    def fake_setup(self, steps, use_proxy=False):
        fwd = _FakeLoopback.instances[-1]
        seen.update(address=(self.vm_ip, self.server_port), target=fwd.target_url, up=fwd.up)
        return True

    ctrl = MagicMock(base_url=DAYTONA_URL)
    task = {"id": "44ee5668", "config": [{"type": "update_browse_history",
                                          "parameters": {"history": []}}]}
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch.object(sandbox, "_verify_launches", return_value=None), \
         patch.object(sandbox, "LoopbackForwarder", _FakeLoopback), \
         patch("desktop_env.controllers.setup.SetupController.setup", fake_setup):
        assert sandbox._run_config(ctrl, task, enable_cdp_forwarder=False) is None

    assert seen == {"address": ("127.0.0.1", 4242), "target": DAYTONA_URL, "up": True}
    assert not _FakeLoopback.instances[-1].up


def test_postconfig_setup_controller_gets_the_scoring_address():
    """The postconfig SetupController is built with the same loopback address as the getters."""
    built = []
    env = MagicMock(action_history=["DONE"])
    ev = {"func": "infeasible", "postconfig": [{"type": "sleep", "parameters": {"seconds": 0}}]}
    with patch.object(osworld_eval, "make_setup_controller",
                      side_effect=lambda *a, **k: built.append(k) or MagicMock()):
        osworld_eval._score(env, ev, "infeasible", DAYTONA_URL, None, MagicMock(), MagicMock(),
                            server_address=("127.0.0.1", 4242))
    assert built and built[0]["server_address"] == ("127.0.0.1", 4242)


def test_getters_setup_controller_gets_the_getter_address():
    adapter = osworld_eval._EnvAdapter(MagicMock(), DAYTONA_URL, ["DONE"],
                                       getter_address=("127.0.0.1", 4242))
    sc = adapter.setup_controller
    assert (sc.vm_ip, sc.server_port) == ("127.0.0.1", 4242)


def test_a_plain_http_controller_is_addressed_directly_without_a_forwarder():
    """A controller already reachable over plain HTTP (the kvm backend's mapped host port) keeps
    its own host: the SetupController's vm_ip also carries the mapped Chrome/VLC ports there, so
    the loopback host would break CDP and VLC."""
    _FakeLoopback.instances = []
    seen = {}

    def fake_setup(self, steps, use_proxy=False):
        seen["address"] = (self.vm_ip, self.server_port)
        return True

    ctrl = MagicMock(base_url="http://10.0.0.5:32801")
    task = {"id": "t", "config": [{"type": "sleep", "parameters": {"seconds": 0}}]}
    with patch.object(sandbox, "_wait_for_desktop_ready", return_value=None), \
         patch.object(sandbox, "_screen_size_mismatch", return_value=None), \
         patch.object(sandbox, "_verify_launches", return_value=None), \
         patch.object(sandbox, "LoopbackForwarder", _FakeLoopback), \
         patch("desktop_env.controllers.setup.SetupController.setup", fake_setup):
        assert sandbox._run_config(ctrl, task, enable_cdp_forwarder=False) is None

    assert seen["address"] == ("10.0.0.5", 32801)
    assert _FakeLoopback.instances == []
