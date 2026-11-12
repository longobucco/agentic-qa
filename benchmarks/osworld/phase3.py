"""Phase 3 experiment table and launcher.

Every Phase 3 run is a (model, arm, task set) group declared below: the guest image (selected by
the pkgfix3 tree suffix, see config.SUFFIX_IMAGES), the protocol knobs, the results tree and the
runs per task all come from here and from data/phase3_sets.json, so a group can be re-run from the
repo alone. Already judged runs are skipped by core.run, so a launch resumes a group.

  python -m benchmarks.osworld.phase3 list
  python -m benchmarks.osworld.phase3 run sonnet e1card2 classF [--dry-run]
"""
import argparse
import json
import os
from pathlib import Path
import sys

from benchmarks.osworld import config

# Which campaign this process runs. "pkgfix3" is the exploratory phase (frozen 12/11: its runs
# carry harness_dirty: true and its arms ran in different time windows); "pkgfix3c" is the X22
# confirmatory campaign, same guest digest, its own results trees. The suffix selects the guest
# image through config.SUFFIX_IMAGES, so a value absent from there would silently run on the
# Phase-2 default digest -- config.resolve_image is what refuses that, and the whitelist here
# stops a typo before it gets that far.
CAMPAIGNS = ("pkgfix3", "pkgfix3c")
SUFFIX = os.environ.get("OSW_PHASE3_SUFFIX", "pkgfix3").strip() or "pkgfix3"
if SUFFIX not in CAMPAIGNS:
    raise SystemExit(f"OSW_PHASE3_SUFFIX={SUFFIX!r}: expected one of {', '.join(CAMPAIGNS)}")

# A set belongs to exactly one campaign. Without this an old set could be launched into a
# confirmatory tree (or a confirmatory set into the exploratory one) and the two would pool.
CAMPAIGN_SETS = {
    "pkgfix3": ("classF", "infeasible", "controls", "classP", "heldoutH", "heldoutP",
                "heldoutPsat"),
    "pkgfix3c": ("heldoutP", "heldoutCsat", "population361", "heldoutC"),
}

COMMON_ENV = {"OSW_POPULATION": "verified361", "OSW_MAX_STEPS": "100",
              "OSW_SCREEN_WIDTH": "1920", "OSW_SCREEN_HEIGHT": "1080", "OSW_IMAGE": ""}

MODELS = {
    "sonnet": {"env": {"OSW_MODEL": "claude-sonnet-5", "OSW_EFFORT": "max",
                       "OSW_MAX_OUTPUT_TOKENS": "128000", "OSW_SYSTEM_SUFFIX": SUFFIX},
               "arm_var": "OSW_ARM", "tree": "agent_computer_sonnet5_effortmax_" + SUFFIX},
    "astra": {"env": {"OSW_ASTRA_REASONING_EFFORT": "max", "OSW_ASTRA_SYSTEM_SUFFIX": SUFFIX},
              "arm_var": "OSW_ASTRA_ARM", "tree": "agent_computer_gpt6astra_max_codex01534_" + SUFFIX},
}

# Arm ("a0" = official protocol, no arm) -> the sets it runs on, with the pre-registration entry
# of the experiment log that defines it. The same table applies to both models.
EXPERIMENTS = {
    "a0": (("classF", "infeasible", "controls", "classP", "heldoutH", "heldoutP", "heldoutPsat",
            "heldoutCsat", "population361", "heldoutC"),
           "X4, X5, X8, X16, X22"),
    "e1card": (("classF", "infeasible", "controls"), "X5"),
    "e1bounce": (("classF", "infeasible", "controls"), "X5"),
    "e1b": (("classP", "heldoutH", "controls", "infeasible"), "X8"),
    # X22 stage 3 screens the population with e1card2, the card already adopted by X10's decision
    # rule on both models -- a choice that predates these data.
    "e1card2": (("classF", "infeasible", "controls",
                 "heldoutP", "heldoutCsat", "population361", "heldoutC"), "X10, X22"),
    "code": (("classF", "controls", "infeasible", "heldoutP", "classP"), "X12, X20"),
    "code2": (("heldoutP", "classP"), "X20 amendment 1, X22"),
    "e1b2": (("heldoutP", "classP", "heldoutPsat", "controls", "infeasible"), "X16, X22"),
}

# Astra's A0 runs of 3ce045a0 belong to the archived pkgfix3 re-run (same tree and configuration),
# so they are not repeated here.
EXCLUDED = {("astra", "a0", "classF"): {"3ce045a0-877b-42aa-8d2c-b4a863336ab8"}}


def load_sets():
    """The declared sets, with `tasks_from` resolved.

    A set may name a file of task ids instead of listing them (population361 points at the frozen
    361-task population), so the population is never duplicated into this file and cannot drift
    away from the id list the campaign lock hashes.
    """
    sets = json.loads((config.DATA_DIR / "phase3_sets.json").read_text())["sets"]
    root = Path(__file__).resolve().parents[2]
    for name, spec in sets.items():
        source = spec.get("tasks_from")
        if not source:
            continue
        ids = [l.strip() for l in (root / source).read_text().splitlines()
               if l.strip() and not l.lstrip().startswith("#")]
        if spec.get("tasks") and spec["tasks"] != ids:
            raise SystemExit(f"set {name!r} declares both tasks and tasks_from, and they differ")
        if len(set(ids)) != len(ids):
            raise SystemExit(f"set {name!r}: {source} has duplicate ids")
        spec["tasks"] = ids
    return sets


def group(model, arm, set_name, sets=None):
    """The env, results tree, task ids and runs per task of one declared group."""
    if model not in MODELS:
        raise SystemExit(f"unknown model {model!r} (known: {', '.join(MODELS)})")
    if arm not in EXPERIMENTS:
        raise SystemExit(f"unknown arm {arm!r} (known: {', '.join(EXPERIMENTS)})")
    if set_name not in EXPERIMENTS[arm][0]:
        raise SystemExit(f"arm {arm!r} is not declared on set {set_name!r} "
                         f"(declared: {', '.join(EXPERIMENTS[arm][0])})")
    if set_name not in CAMPAIGN_SETS[SUFFIX]:
        raise SystemExit(f"set {set_name!r} does not belong to campaign {SUFFIX!r} "
                         f"(its sets: {', '.join(CAMPAIGN_SETS[SUFFIX])}); launching it here would "
                         "pool two campaigns in one results tree")
    s = (sets or load_sets())[set_name]
    m = MODELS[model]
    env = {**COMMON_ENV, **m["env"], m["arm_var"]: "" if arm == "a0" else arm}
    tree = m["tree"] + ("" if arm == "a0" else f"_{arm}") + "_official"
    skip = EXCLUDED.get((model, arm, set_name), set())
    return {"env": env, "tree": tree, "tasks": [t for t in s["tasks"] if t not in skip],
            "runs": s["runs_per_task"]}


def command(g):
    return [sys.executable, "-m", "benchmarks.osworld.run", "--system", g["tree"],
            "--ids", *g["tasks"], "--runs", str(g["runs"])]


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m benchmarks.osworld.phase3")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("model", choices=sorted(MODELS))
    r.add_argument("arm", choices=sorted(EXPERIMENTS))
    r.add_argument("set")
    r.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "list":
        sets = load_sets()
        print(f"campaign {SUFFIX} (OSW_PHASE3_SUFFIX); its sets: "
              f"{', '.join(CAMPAIGN_SETS[SUFFIX])}")
        total = 0
        for arm, (set_names, ref) in EXPERIMENTS.items():
            for set_name in set_names:
                if set_name not in CAMPAIGN_SETS[SUFFIX]:
                    continue   # another campaign's set: group() would refuse it
                for model in MODELS:
                    g = group(model, arm, set_name, sets)
                    total += len(g["tasks"]) * g["runs"]
                    print(f"{model:6} {arm:8} {set_name:14} {len(g['tasks']):3} tasks x "
                          f"{g['runs']}  {ref:22} {g['tree']}")
        print(f"total runs if every group above is run: {total}")
        return
    g = group(a.model, a.arm, a.set)
    cmd = command(g)
    if a.dry_run:
        print(" ".join(f"{k}={v}" for k, v in g["env"].items()), " ".join(cmd))
        return
    os.execve(sys.executable, cmd, {**os.environ, **g["env"]})


if __name__ == "__main__":
    main()
