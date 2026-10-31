import json

import pytest

from benchmarks.osworld.campaign_analysis import code_adoption as ca


@pytest.mark.parametrize("command,cls", [
    ("apt-get install -y spotify-client", "install"),
    ("xdotool key ctrl+s", "gui_via_shell"),
    ("gimp ~/a.png &", "gui_via_shell"),
    ("gsettings set org.gnome.desktop.session idle-delay 0", "settings"),
    ("cat ~/.config/GIMP/2.10/gimprc", "settings"),
    ("sed -i 's/a/b/' notes.txt", "file_edit"),
    ("soffice --headless --convert-to pdf a.docx", "file_edit"),
    ("which bc", "probe"),
    ("ls -la ~/Desktop", "probe"),
    ("true", "other"),
])
def test_classify(command, cls):
    assert ca.classify(command) == cls


def _run(tree, task, n, verdict, steps, commands):
    run = tree / task / f"run_{n}"
    run.mkdir(parents=True)
    (run / "eval.json").write_text(json.dumps({"verdict": verdict}))
    (run / "mcp_state.json").write_text(json.dumps({"started": True, "steps_used": steps}))
    if commands:
        (run / "commands.jsonl").write_text("".join(json.dumps({"command": c}) + "\n" for c in commands))


def test_rows_share_and_audit_flag(tmp_path):
    _run(tmp_path, "aaaaaaaa-1", 1, "SUCCESS", 10, ["gsettings set a b c", "gsettings get a b"])
    _run(tmp_path, "bbbbbbbb-2", 1, "SUCCESS", 8, ["ls ~"])
    _run(tmp_path, "cccccccc-3", 1, "FAILURE", 5, [])
    rows = {r["task"]: r for r in ca.run_rows(tmp_path)}
    assert rows["aaaaaaaa"]["settings"] == 2 and rows["aaaaaaaa"]["command_share"] == 0.2
    assert rows["aaaaaaaa"]["audit"] is True
    assert rows["bbbbbbbb"]["audit"] is False
    assert rows["cccccccc"]["commands"] == 0 and rows["cccccccc"]["audit"] is False
