import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.osworld import campaign, campaign_daytona as dt
from scripts import g_official361_driver as cli

_ROOT = Path(__file__).resolve().parents[3]


class FakeDaytona:
    def __init__(self, sandboxes, fail_list=False, fail_delete=()):
        self.sandboxes, self.fail_list, self.fail_delete = sandboxes, fail_list, set(fail_delete)
        self.deleted, self.queries = [], []

    def list(self, query):
        self.queries.append(query)
        if self.fail_list:
            raise RuntimeError("daytona API down")
        return iter(self.sandboxes)

    def delete(self, sb):
        if sb.id in self.fail_delete:
            raise RuntimeError("busy")
        self.deleted.append(sb.id)


def _sb(id_, label):
    return SimpleNamespace(id=id_, labels={} if label is None else {"osworld.driver_run": label})


def test_sweep_deletes_only_this_runs_sandboxes_even_if_the_filter_leaks():
    fake = FakeDaytona([_sb("mine", "run1"), _sb("other", "run2"), _sb("manual", None)])
    logs = []
    assert dt.sweep("run1", logs.append, client=fake) == 1
    assert fake.deleted == ["mine"]
    assert fake.queries[0].labels == {"osworld.driver_run": "run1"}


def test_sweep_logs_api_errors_instead_of_raising():
    logs = []
    assert dt.sweep("run1", logs.append, client=FakeDaytona([], fail_list=True)) == 0
    assert any("sandbox sweep failed" in m for m in logs)
    fake = FakeDaytona([_sb("a", "run1"), _sb("b", "run1")], fail_delete={"a"})
    assert dt.sweep("run1", logs.append, client=fake) == 1 and fake.deleted == ["b"]


def test_sweep_refuses_an_empty_run_id():
    with pytest.raises(ValueError):
        dt.sweep("", print, client=FakeDaytona([]))


def test_importing_the_daytona_backend_and_cli_does_not_import_config():
    code = ("import sys; import benchmarks.osworld.campaign_daytona, scripts.g_official361_driver; "
            "sys.exit(1 if 'benchmarks.osworld.config' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=_ROOT).returncode == 0


@pytest.mark.parametrize("value", ["", "kvm", "aws"])
def test_cli_refuses_a_missing_or_unknown_backend(monkeypatch, capsys, value):
    monkeypatch.setenv("BACKEND", value)
    called = []
    monkeypatch.setattr(campaign, "main", lambda *a, **k: called.append(a) or 0)
    assert cli.main([]) == 2
    assert "expected one of daytona" in capsys.readouterr().err
    assert called == []


def test_cli_hands_the_daytona_backend_to_the_core(monkeypatch):
    monkeypatch.setenv("BACKEND", "daytona")
    seen = []
    monkeypatch.setattr(campaign, "main", lambda backend, argv: seen.append(backend.NAME) or 0)
    assert cli.main(["--dry-run"]) == 0 and seen == ["daytona"]


def test_a_callers_image_override_is_refused():
    out = campaign.env_conflicts("sonnet", {"OSW_IMAGE": "ghcr.io/x/y@sha256:" + "0" * 64}, dt)
    assert any(c.startswith("OSW_IMAGE=") for c in out)


def test_a_callers_kvm_backend_is_refused_under_the_daytona_backend():
    out = campaign.env_conflicts("sonnet", {"OSW_BACKEND": "kvm"}, dt)
    assert any(c.startswith("OSW_BACKEND='kvm'") for c in out)


def test_child_env_pins_the_image_empty_and_the_backend(monkeypatch):
    env = campaign.child_env("sonnet", {}, dt)
    assert env["OSW_IMAGE"] == "" and env["OSW_BACKEND"] == "daytona"
    assert env["OSW_PROVISION_TIMEOUT"] == "600"


def test_an_empty_osw_image_means_the_pinned_digest(monkeypatch):
    import importlib
    from benchmarks.osworld import config
    monkeypatch.delenv("OSW_IMAGE", raising=False)
    pinned = importlib.reload(config).IMAGE
    monkeypatch.setenv("OSW_IMAGE", "")
    try:
        assert importlib.reload(config).IMAGE == pinned
        assert "@sha256:" in pinned
    finally:
        monkeypatch.delenv("OSW_IMAGE", raising=False)
        importlib.reload(config)


def test_provision_labels_the_sandbox_with_the_driver_run(monkeypatch):
    from benchmarks.osworld.env import sandbox
    seen = {}

    class FakeClient:
        def create(self, params, timeout):
            seen["labels"] = params.labels
            return SimpleNamespace(id="sb")

    monkeypatch.setenv("OSW_DRIVER_RUN", "run-xyz")
    monkeypatch.setattr(sandbox, "_client", lambda: FakeClient())
    monkeypatch.setattr(sandbox, "_ensure_controller_up", lambda sb: "ctrl")
    assert sandbox.provision() == (SimpleNamespace(id="sb"), "ctrl")
    assert seen["labels"] == {"osworld.driver_run": "run-xyz"}


def test_an_image_set_only_in_the_repo_dotenv_is_refused(monkeypatch, capsys):
    # core.run.main loads the repo-root .env in every child, so a knob set only there would
    # otherwise reach the children unchecked (same technique as
    # test_campaign.test_main_checks_knobs_that_come_from_the_repo_dotenv).
    import core.dotenv
    image = "ghcr.io/x/y@sha256:" + "0" * 64
    monkeypatch.setattr(core.dotenv, "load_dotenv",
                        lambda: monkeypatch.setenv("OSW_IMAGE", image))
    monkeypatch.setenv("ARM", "sonnet")
    monkeypatch.delenv("OSW_IMAGE", raising=False)
    assert campaign.main(dt, []) == 2
    assert "OSW_IMAGE" in capsys.readouterr().err


# ---- the sweep failing during a SIGTERM must not change the driver's signal exit code -------

_DAYTONA_SWEEP_ERROR_UNDER_TEST = """
import os, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import benchmarks.osworld.campaign as d
import benchmarks.osworld.campaign_daytona as dt
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


class _FakeDaytonaClient:
    def list(self, query):
        raise RuntimeError("daytona API down")


dt._client = lambda: _FakeDaytonaClient()

_real = subprocess.Popen
n = [0]


def fake_popen(cmd, **kw):
    n[0] += 1
    code = ("import os, signal, sys, time\\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if n[0] == 2 else "")
            + "open(sys.argv[1], 'w').write(str(os.getpid()))\\ntime.sleep(120)\\n")
    return _real([sys.executable, "-c", code, str(tmp / f"child{n[0]}.pid")])


d.subprocess.Popen = fake_popen
sys.exit(d.main(dt, []))
"""


def _start_daytona_sweep_error_driver(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text(_DAYTONA_SWEEP_ERROR_UNDER_TEST)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OSW_")}
    env.update({"ARM": "sonnet", "PARALLEL": "2"})
    return subprocess.Popen([sys.executable, str(helper), str(tmp_path), str(_ROOT)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_the_sweep_error_does_not_change_the_signal_exit_code(tmp_path):
    proc = _start_daytona_sweep_error_driver(tmp_path)
    pid_files = [tmp_path / "child1.pid", tmp_path / "child2.pid"]
    import time
    deadline = time.time() + 30
    while not all(p.exists() and p.read_text() for p in pid_files):
        assert time.time() < deadline and proc.poll() is None, proc.communicate()[0]
        time.sleep(0.1)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM, out
    log = (tmp_path / "scripts" / "g_official361_sonnet_daytona.log").read_text()
    assert "sandbox sweep failed" in log
