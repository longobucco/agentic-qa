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
import sys

from benchmarks.osworld import config

SUFFIX = "pkgfix3"
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
    "a0": (("classF", "infeasible", "controls", "classP", "heldoutH"), "X4, X5, X8"),
    "e1card": (("classF", "infeasible", "controls"), "X5"),
    "e1bounce": (("classF", "infeasible", "controls"), "X5"),
    "e1b": (("classP", "heldoutH", "controls", "infeasible"), "X8"),
    "e1card2": (("classF", "infeasible", "controls"), "X10"),
    "code": (("classF", "controls", "infeasible"), "X12"),
}

# Astra's A0 runs of 3ce045a0 belong to the archived pkgfix3 re-run (same tree and configuration),
# so they are not repeated here.
EXCLUDED = {("astra", "a0", "classF"): {"3ce045a0-877b-42aa-8d2c-b4a863336ab8"}}


def load_sets():
    return json.loads((config.DATA_DIR / "phase3_sets.json").read_text())["sets"]


def group(model, arm, set_name, sets=None):
    """The env, results tree, task ids and runs per task of one declared group."""
    if model not in MODELS:
        raise SystemExit(f"unknown model {model!r} (known: {', '.join(MODELS)})")
    if arm not in EXPERIMENTS:
        raise SystemExit(f"unknown arm {arm!r} (known: {', '.join(EXPERIMENTS)})")
    if set_name not in EXPERIMENTS[arm][0]:
        raise SystemExit(f"arm {arm!r} is not declared on set {set_name!r} "
                         f"(declared: {', '.join(EXPERIMENTS[arm][0])})")
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
        for arm, (set_names, ref) in EXPERIMENTS.items():
            for set_name in set_names:
                for model in MODELS:
                    g = group(model, arm, set_name, sets)
                    print(f"{model:6} {arm:8} {set_name:10} {len(g['tasks']):2} tasks x "
                          f"{g['runs']}  {ref:10} {g['tree']}")
        return
    g = group(a.model, a.arm, a.set)
    cmd = command(g)
    if a.dry_run:
        print(" ".join(f"{k}={v}" for k, v in g["env"].items()), " ".join(cmd))
        return
    os.execve(sys.executable, cmd, {**os.environ, **g["env"]})


if __name__ == "__main__":
    main()
