"""Adoption and shortcut audit for the declared code arm (EXPERIMENTS.md X12).

For every run of a results tree it reads commands.jsonl (one line per run_command call, written by
mcp/command_tool.py), classifies each command and writes, per run: verdict, steps used, commands by
class, and the share of steps taken through run_command. SUCCESS runs whose commands write files,
change settings or install software are listed for the pre-registered manual trajectory audit
(shortcuts the instruction rules out that the official checker cannot see).

    python code_adoption.py <results_tree> <out_dir>
"""
import csv
import json
import re
import sys
from pathlib import Path

# First match wins: a command that installs software is "install" even if it also lists files.
CLASSES = (
    ("install", r"\b(apt(-get)?\s+install|pip3?\s+install|snap\s+install|flatpak\s+install|dpkg\s+-i)\b"
                r"|\b(wget|curl)\b.*\b(https?://)"),
    ("gui_via_shell", r"\b(xdotool|wmctrl|pyautogui|gnome-screenshot|scrot|xdg-open|xwd)\b"
                      r"|\bimport\s+-window\b|(^|[;&|]\s*)(gimp|libreoffice|soffice|google-chrome|code|"
                      r"thunderbird|vlc)\b(?!.*--headless)"),
    ("settings", r"\b(gsettings|dconf|xfconf-query|pactl|xrandr|xdg-mime|update-alternatives|"
                 r"timedatectl)\b|\.config/|gimprc|registrymodifications\.xcu|sessionrc"),
    ("file_edit", r"\bsed\s+-i\b|\btee\b|[^2]>>?\s*\S|\b(cp|mv|rm|mkdir|touch|unzip|zip|chmod|chown)\b"
                  r"|--headless|\bopenpyxl\b|\bpptx\b|\bdocx\b|open\([^)]*['\"][wa]"),
    ("probe", r"\b(ls|cat|grep|find|which|ps|pgrep|stat|file|head|tail|wc|echo|pwd|id|whoami)\b"
              r"|command\s+-v|--version|--help|dpkg\s+-l"),
)
_COMPILED = [(name, re.compile(rx)) for name, rx in CLASSES]
AUDIT_CLASSES = {"install", "settings", "file_edit"}


def classify(command):
    for name, rx in _COMPILED:
        if rx.search(command or ""):
            return name
    return "other"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def run_rows(tree):
    for run in sorted(Path(tree).glob("*/run_*")):
        verdict = (_read_json(run / "eval.json") or {}).get("verdict")
        if verdict not in ("SUCCESS", "FAILURE"):
            continue
        steps = (_read_json(run / "mcp_state.json") or {}).get("steps_used")
        cmds = []
        log = run / "commands.jsonl"
        if log.exists():
            for line in log.read_text().splitlines():
                try:
                    cmds.append(json.loads(line))
                except ValueError:
                    pass
        counts = {name: 0 for name, _ in CLASSES} | {"other": 0}
        for c in cmds:
            counts[classify(c.get("command"))] += 1
        yield {"task": run.parent.name[:8], "run": run.name, "verdict": verdict, "steps": steps,
               "commands": len(cmds), **counts,
               "command_share": round(len(cmds) / steps, 3) if steps else None,
               "audit": verdict == "SUCCESS" and any(counts[k] for k in AUDIT_CLASSES)}


def main(tree, out_dir):
    rows = list(run_rows(tree))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(out / "code_adoption.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    used = sum(1 for r in rows if r["commands"])
    print(f"{len(rows)} judged runs, {used} used run_command, "
          f"{sum(r['audit'] for r in rows)} SUCCESS runs to audit by hand")


if __name__ == "__main__":
    main(*sys.argv[1:3])
