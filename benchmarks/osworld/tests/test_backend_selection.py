"""Task 9: which environment/preflight each runner gets by config.BACKEND. With OSW_BACKEND
unset (or "daytona"), every runner's environment/preflight is exactly what it was before this
task existed -- these tests pin that as well as the new kvm behavior."""
import importlib
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from benchmarks.osworld import config


class _Names:
    """Plain-string snapshot of the system names built() used, taken BEFORE the modules are
    reloaded back to the ambient environment -- config.reload() mutates the SAME module object
    in place, so holding onto the module itself (rather than copying its attributes out) would
    silently go stale the moment the restoring reload runs, same trap test_protocol_config.py's
    own _reload avoids by extracting a plain tuple first."""


@contextmanager
def _reload(**env):
    """Reload config then benchmark (build() reads config.* at call time, but the reload pattern
    of test_protocol_config.py reloads both, and benchmark.py itself has no per-import state that
    would need it) and yield (built, names) while OSW_BACKEND etc are still patched -- a
    preflight() call needs config.BACKEND to still read the patched value, not the ambient one,
    so restoring happens only after the `with` block exits. Restores both modules to the ambient
    environment afterward so the rest of the suite sees today's config/benchmark."""
    base = {"OSW_MODEL": "claude-sonnet-5", "OSW_BACKEND": "", "OSW_KVM_QCOW2_SHA256": ""}
    base.update(env)
    with patch.dict(os.environ, base):
        c = importlib.reload(config)
        from benchmarks.osworld import benchmark as b
        b = importlib.reload(b)
        built = b.build()
        names = _Names()
        names.SYSTEM_NAME = c.SYSTEM_NAME
        names.ASTRA_SYSTEM_NAME = c.ASTRA_SYSTEM_NAME
        names.ASTRA_OPENBOOK_SYSTEM_NAME = c.ASTRA_OPENBOOK_SYSTEM_NAME
        names.VR_SYSTEM_NAME = c.VR_SYSTEM_NAME
        names.BACKEND = c.BACKEND
        try:
            yield built, names
        finally:
            pass
    importlib.reload(config)
    from benchmarks.osworld import benchmark as b2
    importlib.reload(b2)


def test_daytona_backend_keeps_osworld_environment():
    from benchmarks.osworld.env.sandbox import osworld_environment
    with _reload() as (built, c):
        assert built.runners[c.SYSTEM_NAME].environment is osworld_environment
        assert built.runners[c.ASTRA_SYSTEM_NAME].environment is osworld_environment


def test_kvm_backend_selects_kvm_environment_for_sonnet_and_astra():
    from benchmarks.osworld.env import kvm_vm
    with _reload(OSW_BACKEND="kvm", OSW_KVM_QCOW2_SHA256="deadbeef") as (built, c):
        assert built.runners[c.SYSTEM_NAME].environment is kvm_vm.kvm_environment
        assert built.runners[c.ASTRA_SYSTEM_NAME].environment is kvm_vm.kvm_environment


def test_kvm_backend_does_not_touch_openbook_or_verify_replan_environment():
    from benchmarks.osworld.env.sandbox import osworld_environment, osworld_openbook_environment
    with _reload(OSW_BACKEND="kvm", OSW_KVM_QCOW2_SHA256="deadbeef") as (built, c):
        assert (built.runners[c.ASTRA_OPENBOOK_SYSTEM_NAME].environment
                is osworld_openbook_environment)
        assert built.runners[c.VR_SYSTEM_NAME].environment is osworld_environment


def test_openbook_preflight_refuses_kvm_backend():
    with _reload(OSW_BACKEND="kvm", OSW_KVM_QCOW2_SHA256="deadbeef") as (built, c):
        with pytest.raises(SystemExit):
            built.runners[c.ASTRA_OPENBOOK_SYSTEM_NAME].preflight()


def test_openbook_preflight_unaffected_by_daytona_backend():
    """With the flag unset, the open-book runner's own OFFICIAL refusal / campaign_check path is
    unchanged -- this only proves the new kvm branch isn't taken, not the whole preflight (that's
    test_open_book_preflight.py's job)."""
    with _reload() as (built, c):
        assert c.BACKEND == "daytona"


def test_verify_replan_preflight_refuses_kvm_backend():
    with _reload(OSW_BACKEND="kvm", OSW_KVM_QCOW2_SHA256="deadbeef") as (built, c):
        assert built.runners[c.VR_SYSTEM_NAME].preflight is not None
        with pytest.raises(SystemExit):
            built.runners[c.VR_SYSTEM_NAME].preflight()


def test_verify_replan_preflight_ok_under_daytona():
    with _reload() as (built, c):
        # No SystemExit -- verify_replan has no other preflight requirement today.
        built.runners[c.VR_SYSTEM_NAME].preflight()


def test_kvm_combined_preflight_runs_kvm_check_before_the_runners_own(monkeypatch):
    """_with_kvm_preflight(pf): kvm_vm.preflight() runs first (cheapest failure first), then the
    runner's own preflight -- and a kvm failure must short-circuit before the runner's own
    preflight is even called."""
    from benchmarks.osworld import benchmark as b
    from benchmarks.osworld.env import kvm_vm

    calls = []
    monkeypatch.setattr(config, "BACKEND", "kvm")
    monkeypatch.setattr(kvm_vm, "preflight", lambda: calls.append("kvm"))

    def own():
        calls.append("own")

    combined = b._with_kvm_preflight(own)
    combined()
    assert calls == ["kvm", "own"]

    def kvm_fails():
        raise SystemExit("kvm preflight: no")
    monkeypatch.setattr(kvm_vm, "preflight", kvm_fails)
    calls.clear()
    with pytest.raises(SystemExit):
        b._with_kvm_preflight(own)()
    assert calls == []


def test_with_kvm_preflight_is_a_noop_wrapper_under_daytona(monkeypatch):
    from benchmarks.osworld import benchmark as b
    monkeypatch.setattr(config, "BACKEND", "daytona")
    calls = []
    combined = b._with_kvm_preflight(lambda: calls.append("own"))
    combined()
    assert calls == ["own"]
