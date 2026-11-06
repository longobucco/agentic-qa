"""V1 (EXPERIMENTS.md X15): read-only final-state capture after the agent has finished."""
import json

from benchmarks.osworld.runners import common


class FakeCtrl:
    def __init__(self, fail_on=None):
        self.commands, self.fail_on = [], fail_on

    def execute(self, command, *, shell=False, timeout=120, **_):
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            raise RuntimeError("guest down")
        return "out:" + command[-40:]


def test_final_state_writes_every_item_and_a_manifest(tmp_path):
    ctrl = FakeCtrl()
    common._capture_final_state(ctrl, tmp_path)
    m = json.loads((tmp_path / "final_state" / "manifest.json").read_text())
    names = {i["name"] for i in m["items"]}
    assert "dconf_dump.ini" in names and "file__.config__libreoffice__4__user__registrymodifications.xcu" in names
    assert len(m["items"]) == len(common.FINAL_STATE_COMMANDS) + len(common.FINAL_STATE_FILES)
    assert all((tmp_path / "final_state" / i["name"]).exists() for i in m["items"])


def test_final_state_commands_are_read_only_and_capped(tmp_path):
    ctrl = FakeCtrl()
    common._capture_final_state(ctrl, tmp_path)
    for c in ctrl.commands:
        assert "head -c" in c
        for verb in (" rm ", " mv ", " cp ", "gsettings set", "dconf write", "dconf reset", " > ", ">>"):
            assert verb not in c, (verb, c)
    assert any('"$HOME/.config/google-chrome/Local State"' in c for c in ctrl.commands)


def test_final_state_records_failures_without_raising(tmp_path):
    common._capture_final_state(FakeCtrl(fail_on="dconf dump"), tmp_path)
    m = json.loads((tmp_path / "final_state" / "manifest.json").read_text())
    bad = [i for i in m["items"] if "error" in i]
    assert len(bad) == 1 and bad[0]["name"] == "dconf_dump.ini"
