"""DEV1 pilot gate: the checks the first few runs must pass before the rest of the tranche runs.

    OSW_ASTRA_SYSTEM_SUFFIX=pkgfix3cdev1 OSW_SYSTEM_SUFFIX=pkgfix3cdev1 \\
      .venv/bin/python -m benchmarks.osworld.dev1_pilot [--results DIR]

The pilot is `PILOT_RUNS` of the forty, chosen by the register's own order, counted inside the
tranche and never in addition to it. Its job is not to produce evidence about the agent: it is to
prove that a DEV run collects what the checkpoint will need, lands where it is supposed to land,
and costs what the schedule assumes.

Written and tested BEFORE any run exists, so the bar cannot be adjusted once results are visible.

Every condition below is a halt, not a warning: on a failure the tranche stops, the runs already
produced are kept exactly as they are, and nothing is re-run silently. A retry the harness
performs by itself is visible too -- `infra_error.json` is an append-only log of attempts, so the
report counts them rather than letting a quiet second attempt look like a clean first one.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from benchmarks.osworld import config, diagnostics          # noqa: E402

REGISTER = HERE / "dev1_tranche_register.json"
LOCK = HERE / "x22_campaign_lock.json"
PILOT_RUNS = 4
DEV_SUFFIX = "pkgfix3cdev1"


def dev_tree():
    return config.ASTRA_SYSTEM_NAME


# The pilot exercises the environments most likely to break, not the best known ones: vscode has
# never been run at all, thunderbird has one judged task of 23, writer three of 34. The fourth is
# a reference application so a failure can be read as "this environment" rather than "everything".
PILOT_APP_ORDER = ("vscode", "thunderbird", "libreoffice_writer", "libreoffice_calc")


def pilot_tasks(register=None):
    """One task per application, in PILOT_APP_ORDER, taking the first of that application in
    register order. Deterministic, and fixed before any result exists."""
    reg = register or json.loads(REGISTER.read_text())
    out = []
    for app in PILOT_APP_ORDER:
        for t in reg["tasks"]:
            if t["app"] == app:
                out.append(t)
                break
        if len(out) == PILOT_RUNS:
            break
    return out


def check_run(run_dir, lock, tree):
    """Reasons this pilot run does not clear the gate ([] = clear)."""
    bad = []
    d = diagnostics.collect(run_dir, tree=tree)
    result = diagnostics._json(Path(run_dir) / "result.json") or {}
    prov = result.get("provenance") or {}

    if d["reconstructible"] is not True:
        bad.append(f"not reconstructible: {d['gaps']}")
    if not d["collected"]["rollout_archive"].get("present"):
        bad.append("Codex rollout not archived: the trajectory would exist only under CODEX_HOME "
                   f"({d['collected']['rollout_archive'].get('reason')})")
    if not d["collected"]["final_screenshot"].get("present"):
        bad.append("no final screenshot")
    if d["step_counter_resets"] != 0:
        bad.append(f"the MCP server was restarted mid-run ({d['step_counter_resets']})")
    reason = (d.get("artefact_capture") or {}).get("reason", "")
    if not reason or reason.startswith("not recorded"):
        bad.append("artefact capture not diagnosed: an empty manifest would be unexplained (B7)")
    ph = d.get("phase_seconds") or {}
    missing = [k for k in ("provision", "setup", "agent") if ph.get(k) is None]
    if missing:
        bad.append(f"phase timing missing: {missing}")

    # provenance: the right guest, the declared protocol, and NOT an X22 tree
    if prov.get("image") != lock["image"]:
        bad.append(f"image {prov.get('image')!r} is not the locked guest")
    if prov.get("observation") != "screenshot":
        bad.append(f"observation {prov.get('observation')!r}: a0 observes screenshots only")
    if prov.get("arm") not in (None, ""):
        bad.append(f"arm {prov.get('arm')!r}: the diagnostic screen runs a0")
    if prov.get("harness_dirty"):
        bad.append("harness_dirty: the tree was not clean")
    return bad, d


def scan(results_dir=None, register=None):
    results_dir = Path(results_dir) if results_dir else config.RESULTS_DIR
    lock = json.loads(LOCK.read_text())
    reg = register or json.loads(REGISTER.read_text())
    tree = dev_tree()
    rep = {"tree": tree, "pilot_runs": [], "violations": [], "x22_trees_touched": [],
           "attempts": {}, "checked": 0}

    if DEV_SUFFIX not in tree:
        rep["violations"].append(
            {"run": "-", "reason": f"tree {tree!r} does not carry the DEV1 suffix: set "
                                   f"OSW_ASTRA_SYSTEM_SUFFIX={DEV_SUFFIX} and "
                                   f"OSW_SYSTEM_SUFFIX={DEV_SUFFIX}"})
    for g in lock["groups"]:                 # a DEV run must never appear in a campaign tree
        if (results_dir / g["tree"]).exists():
            rep["x22_trees_touched"].append(g["tree"])

    dev_ids = {t["task"] for t in reg["tasks"]}
    root = results_dir / tree
    for t in pilot_tasks(reg):
        for run in sorted((root / t["task"]).glob("run_*")) if root.exists() else []:
            rep["checked"] += 1
            bad, d = check_run(run, lock, tree)
            rep["pilot_runs"].append({"task": t["task"][:8], "app": t["app"], "run": run.name,
                                      "run_key": d["run_key"], "verdict": d["verdict"],
                                      "steps": d["steps_used"],
                                      "phase_seconds": d["phase_seconds"], "clear": not bad})
            rep["violations"] += [{"run": f"{t['task'][:8]}/{run.name}", "reason": r}
                                  for r in bad]
            ie = run / "infra_error.json"
            if ie.exists():
                # An append-only log of attempts, so the report counts them rather than letting a
                # quiet second attempt look like a clean first one -- but it is a pretty-printed
                # JSON array, and counting its lines reported ~9 attempts per attempt. Parse it,
                # and fall back to the line count only if it is not valid JSON.
                raw = ie.read_text()
                try:
                    rows = json.loads(raw)
                    n = len(rows if isinstance(rows, list) else [rows])
                except Exception:
                    n = sum(1 for line in raw.splitlines() if line.strip())
                rep["attempts"][f"{t['task'][:8]}/{run.name}"] = n
    if root.exists():
        stray = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in dev_ids)
        if stray:
            rep["violations"].append({"run": "-", "reason": f"tasks outside the register: {stray}"})
    rep["clear"] = not rep["violations"] and rep["checked"] >= 1
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = scan(a.results)
    if a.json:
        print(json.dumps(rep, indent=2)); return
    print(f"DEV1 pilot gate — tree {rep['tree']}\n")
    print(f"  pilot tasks (register order, first run of each application):")
    for t in pilot_tasks():
        print(f"    {t['task'][:8]}  {t['app']:22} ({t['pool']})")
    print(f"\n  runs checked: {rep['checked']} of {PILOT_RUNS}")
    for r in rep["pilot_runs"]:
        ph = r["phase_seconds"] or {}
        print(f"    {'OK ' if r['clear'] else 'HALT'} {r['task']} {r['app']:20} "
              f"{str(r['verdict']):8} steps={r['steps']} "
              f"provision={ph.get('provision')}s setup={ph.get('setup')}s agent={ph.get('agent')}s")
    if rep["attempts"]:
        print(f"\n  runs the harness retried by itself (visible, never silent): {rep['attempts']}")
    if rep["x22_trees_touched"]:
        print(f"\n  !! X22 campaign trees present in this results dir: "
              f"{rep['x22_trees_touched']}")
    print()
    if rep["violations"]:
        print(f"  HALT — {len(rep['violations'])} condition(s) not met. Keep the runs as they "
              f"are; do not re-run.")
        for v in rep["violations"][:20]:
            print(f"    {v['run']}: {v['reason']}")
    elif rep["clear"]:
        print("  pilot gate CLEAR — the remaining runs of the tranche may proceed "
              "(a separate authorisation).")
    else:
        print("  no pilot run found yet.")


if __name__ == "__main__":
    main()
