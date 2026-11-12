"""Gate A: the contract check, run before a group is launched. Zero runs, zero quota.

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.gates.contract astra a0 heldoutP

Exit 0 means the configuration on disk is the one the frozen lock declares. Any other exit code
means nothing is launched. This is the cheapest of the three gates and the only one that can stop a
misconfiguration before it costs a single run.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from benchmarks.osworld import arms, config, phase3

LOCK = Path(__file__).resolve().parent.parent / "x22_campaign_lock.json"


def _git(*args):
    root = Path(__file__).resolve().parents[3]
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def check(model, arm, set_name, lock=None):
    """Every reason this group must not be launched, as a list of strings ([] = clear)."""
    lock = lock or json.loads(LOCK.read_text())
    bad = []

    # --- the harness itself -------------------------------------------------------------------
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty is None:
        bad.append("git is not readable: harness_commit/harness_dirty would be null in provenance")
    elif dirty:
        bad.append(f"working tree is dirty, so every run would record harness_dirty: true "
                   f"({len(dirty.splitlines())} file(s))")
    tag = lock["harness_tag"]
    at_tag = _git("describe", "--tags", "--exact-match", "HEAD")
    if at_tag != tag:
        bad.append(f"HEAD is not at the frozen tag {tag!r} (describe says {at_tag!r}): the runs "
                   "would record a commit the lock does not name")

    # --- the campaign -------------------------------------------------------------------------
    if phase3.SUFFIX != lock["tree_suffix"]:
        bad.append(f"OSW_PHASE3_SUFFIX is {phase3.SUFFIX!r}, the lock declares "
                   f"{lock['tree_suffix']!r}")
    declared = [g for g in lock["groups"]
                if (g["model"], g["arm"], g["set"]) == (model, arm, set_name)]
    if not declared:
        bad.append(f"group ({model}, {arm}, {set_name}) is not declared in the lock; the planned "
                   f"groups are: " + ", ".join(f"{g['arm']}/{g['set']}" for g in lock["groups"]))

    # --- model, runtime, guest, evaluator -----------------------------------------------------
    for field, actual in (("model", config.ASTRA_MODEL),
                          ("reasoning_effort", config.ASTRA_REASONING_EFFORT),
                          ("codex_cli_version", config.ASTRA_CODEX_VERSION),
                          ("max_steps", config.MAX_STEPS),
                          ("screen_size", f"{config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}")):
        if lock[field] != actual:
            bad.append(f"{field}: config says {actual!r}, the lock {lock[field]!r}")
    image = config.SUFFIX_IMAGES.get(lock["tree_suffix"])
    if image != lock["image"]:
        bad.append(f"the image of suffix {lock['tree_suffix']!r} is {image!r}, the lock says "
                   f"{lock['image']!r}")

    # --- the arm text -------------------------------------------------------------------------
    if arm != "a0":
        frozen = lock["arm_addendum_sha256"].get(arm)
        actual = arms.provenance(arm)["arm_addendum_sha256"]
        if frozen != actual:
            bad.append(f"arm {arm!r} text has changed: arms.py gives {actual}, the lock {frozen}")
        if list(arms.tools(arm)) != lock["arm_tools"].get(arm, []):
            bad.append(f"arm {arm!r} tools changed: {list(arms.tools(arm))} vs "
                       f"{lock['arm_tools'].get(arm)}")

    # --- the task set -------------------------------------------------------------------------
    if declared:
        try:
            g = phase3.group(model, arm, set_name)
        except SystemExit as e:
            bad.append(f"phase3.group refuses this group: {e}")
        else:
            if len(g["tasks"]) != declared[0]["tasks"]:
                bad.append(f"set {set_name!r} has {len(g['tasks'])} tasks, the lock declares "
                           f"{declared[0]['tasks']}")
            if g["runs"] != declared[0]["runs_per_task"]:
                bad.append(f"set {set_name!r} runs_per_task is {g['runs']}, the lock declares "
                           f"{declared[0]['runs_per_task']}")
            if g["tree"] != declared[0]["tree"]:
                bad.append(f"results tree would be {g['tree']!r}, the lock declares "
                           f"{declared[0]['tree']!r}")

    # --- the look design --------------------------------------------------------------------
    if (lock["stage3"].get("boundaries") or {}).get("nominal_alpha") is None:
        bad.append("stage-3 look boundaries are not frozen in the lock (run gates.calibrate)")
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m benchmarks.osworld.gates.contract")
    ap.add_argument("model")
    ap.add_argument("arm")
    ap.add_argument("set")
    a = ap.parse_args(argv)
    bad = check(a.model, a.arm, a.set)
    if not bad:
        print(f"gate A ok: {a.model} {a.arm} {a.set}")
        return 0
    print(f"gate A REFUSES {a.model} {a.arm} {a.set}:", file=sys.stderr)
    for reason in bad:
        print(f"  - {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
