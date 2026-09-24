import importlib
import os
from unittest.mock import patch

from benchmarks.osworld import config


def _reload(**env):
    base = {"OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "", "OSW_SYSTEM_SUFFIX": "",
            "OSW_ASTRA_SYSTEM_SUFFIX": "", "OSW_ZOOM_BATCH": "0", "OSW_GROUNDING": "0",
            "OSW_PROTOCOL": "", "OSW_BACKEND": "", "OSW_TASK_TIMEOUT": ""}
    base.update(env)
    with patch.dict(os.environ, base):
        c = importlib.reload(config)
        vals = (c.SYSTEM_NAME, c.ASTRA_SYSTEM_NAME, c.PROTOCOL, c.BACKEND, c.TASK_TIMEOUT)
    importlib.reload(config)
    return vals


def test_official_kvm_names_and_timeout():
    name, astra, protocol, backend, timeout = _reload(
        OSW_PROTOCOL="official", OSW_BACKEND="kvm", OSW_EFFORT="max",
        OSW_SYSTEM_SUFFIX="protocol361", OSW_ASTRA_SYSTEM_SUFFIX="protocol361")
    assert name == "agent_computer_sonnet5_effortmax_protocol361_official_kvm"
    assert astra == "agent_computer_gpt6astra_max_codex01534_protocol361_official_kvm"
    assert protocol == "official" and backend == "kvm" and timeout == 14400


def test_unknown_values_are_refused():
    import pytest
    with pytest.raises(SystemExit):
        _reload(OSW_PROTOCOL="oficial")
    with pytest.raises(SystemExit):
        _reload(OSW_BACKEND="aws")


def test_provenance_records_protocol_and_backend(monkeypatch):
    from benchmarks.osworld.runners import common
    monkeypatch.setattr(config, "PROTOCOL", "official")
    monkeypatch.setattr(config, "BACKEND", "kvm")
    rec = common._provenance({"id": "t"}, ctrl=None, started_at="x")
    assert rec["protocol"] == "official" and rec["backend"] == "kvm"
    assert "kvm_qcow2_sha256" in rec and "kvm_image" in rec
