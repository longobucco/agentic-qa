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


class _EvalStateCtrl(FakeCtrl):
    """FakeCtrl plus the two calls the eval-state capture makes beyond commands."""

    def __init__(self):
        super().__init__()
        self.reads = []

    def read_file(self, path):
        self.reads.append(path)
        return b"postconfig artifact"

    def screenshot(self):
        return b"\x89PNG"


_FAIL_TASK = {"evaluator": {"func": "exact_match",
                            "result": {"type": "vm_file", "path": "/home/user/log.txt",
                                       "dest": "eval_artifact"}}}


def test_declared_fail_capture_skips_the_result_spec_and_keeps_the_rest(tmp_path):
    """What an agent-declared FAIL has to skip is the postconfig read, and only that.

    Official scoring short-circuits on a declared FAIL before its postconfig can create the
    vm_file, so fetching it would 404. The audit screenshot and the structured dump do not depend
    on postconfig: skipping them too left 224 archived FAIL runs with no final state (13/11).
    """
    ctrl = _EvalStateCtrl()
    captured = common._capture_eval_state(ctrl, _FAIL_TASK, tmp_path, False)
    assert captured is None, "the evaluator's result must not be read on a declared FAIL"
    assert ctrl.reads == [] and not (tmp_path / "eval_artifact").exists()
    assert (tmp_path / "final.png").read_bytes() == b"\x89PNG"
    assert (tmp_path / "final_state" / "manifest.json").exists()


def test_the_default_capture_still_reads_the_result_spec(tmp_path):
    """Non-interference: every other caller passes no flag and must be unaffected."""
    ctrl = _EvalStateCtrl()
    captured = common._capture_eval_state(ctrl, _FAIL_TASK, tmp_path)
    assert captured == "postconfig artifact"
    assert ctrl.reads == ["/home/user/log.txt"]
    assert (tmp_path / "eval_artifact").read_bytes() == b"postconfig artifact"
    assert (tmp_path / "final.png").exists()


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
