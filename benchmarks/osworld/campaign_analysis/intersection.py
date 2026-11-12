"""The intersection of the ledgers: how many failures is a new tool actually allowed to reach?

    .venv/bin/python benchmarks/osworld/campaign_analysis/intersection.py [--out tables]

Three counts have been quoted side by side as if they shared a denominator, and they do not:

  F3        203 labelled Astra failures (pkgfix3 trees, X11/F1 controllability labels)
  F2        213 Astra failures classified by how the run ended
  tool_pool 147 runs with BOTH a result and a gold artefact -- **across both models**, so its
            49% content / 32% shape split was never an Astra-only statement

Sizing an arm from any one of them overcounts. What a write-side or feedback tool can reach is the
*intersection*: a failure that the agent could have controlled, that ended with the agent believing
it was done, on an application the tool would support, with an artefact difference of a kind the
tool could have prevented. This file computes that intersection run by run and task by task, from
artefacts already on disk, and reports it as a funnel.

Two guards against fooling ourselves, both from the review of 2026-11-12:

  * **the same signature is counted on SUCCESS runs too.** A difference class that also shows up in
    runs the checker passed is not diagnostic -- it is what a correct run looks like. F1 already
    found two runs that correctly PASS while differing from the gold by 296 rendering lines, all of
    them formulas, which `sheet_data` does not read.
  * **`mixed` is reported next to `controllable`, never merged into it.** The labels were assigned
    against the action surface of the time; they bound a priority pool, not a theorem.

Nothing here is an estimate of effect. It is the size of the pool an effect would have to come from.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E                                  # noqa: E402
from f2_diagnostics import end_of_run_class              # noqa: E402
from f3_ledger import G17, labels                        # noqa: E402
from tool_pool import classify                           # noqa: E402

sys.path.insert(0, str(HERE.parents[2]))
from benchmarks.osworld import tasks as _tasks           # noqa: E402

RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"

# `related_apps` is not normalised upstream: libreoffice_calc / "libreoffice calc" / calc are the
# same application, and vscode / vs_code likewise. A tool is built per application, so the counts
# have to be per application too.
APP_ALIASES = {"libreoffice calc": "libreoffice_calc", "calc": "libreoffice_calc",
               "vs_code": "vscode", "OS": "os"}

# What a Calc-side write tool (UNO primitives) could plausibly support, as opposed to merely
# observe. Declared here so the funnel's last step is explicit and arguable, not hidden.
WRITE_TOOL_APPS = {"libreoffice_calc"}

# Difference classes a write-side tool is aimed at: it constructs the object instead of assembling
# it through the GUI. `identical_rendering` is excluded -- the difference is outside the rendering,
# so nothing a renderer-shaped tool does would have shown it.
ADDRESSABLE_KINDS = {"shape", "content", "formatting", "structural"}


def app_of(task):
    apps = task.get("related_apps") or []
    first = apps[0] if apps else "unknown"
    return APP_ALIASES.get(first, first)


def artefact_state(run_dir, ev):
    """('pair'|'result_only'|'gold_only'|'none', diff class or None).

    'none' is the B7 signal: the evaluator kept nothing, so there is no artefact to have got
    wrong -- 94 of 403 failures whose checker reads a vm_file, against 0 of 531 successes.
    """
    arts = [a for a in ev.get("eval_artifacts", []) if a.get("kept")]
    res = [a["name"] for a in arts if not a.get("is_gold")]
    gold = [a["name"] for a in arts if a.get("is_gold")]
    if not res and not gold:
        return "none", None
    if not gold:
        return "result_only", None
    if not res:
        return "gold_only", None
    d = run_dir / "eval_artifacts"
    kinds = []
    for r, g in zip(res, gold):
        if (d / r).is_file() and (d / g).is_file():
            kinds.append(classify(E.render_file(d / r), E.render_file(d / g)))
    if not kinds:
        return "pair", None
    order = ["empty_result", "structural", "shape", "content", "formatting",
             "identical_rendering"]
    return "pair", min(kinds, key=lambda k: order.index(k) if k in order else 99)


def rows():
    sets = json.loads(SETS.read_text())["sets"]
    setof = {t: n for n, s in sets.items() if n != "population361" for t in s["tasks"]}
    lab = labels()
    byid = {t["id"]: t for t in _tasks.load_tasks()}
    out = []
    for p in sorted(RESULTS.glob("agent_computer_*_official/*/run_*/eval.json")):
        tree = p.parents[2].name
        if "pkgfix3" not in tree or "nullagent" in tree:
            continue
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else None)
        if model is None:
            continue
        try:
            ev = json.loads(p.read_text())
            result = json.loads((p.parent / "result.json").read_text())
        except (OSError, ValueError):
            continue
        if ev.get("verdict") not in ("SUCCESS", "FAILURE"):
            continue
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("_official", "").strip("_") or "a0")
        task = p.parents[1].name
        state, kind = artefact_state(p.parent, ev)
        out.append({
            "model": model, "arm": arm, "set": setof.get(task, "?"), "task8": task[:8],
            "run": p.parent.name, "verdict": ev["verdict"],
            "app": app_of(byid.get(task, {})),
            "end": end_of_run_class(p.parent, result),
            "control": lab.get(task[:8], "unlabelled"),
            "g17": task[:8] in G17,
            "artefact": state, "kind": kind or "",
            "steps": result.get("agent_steps_used"),
        })
    return out


def _funnel(rs, model):
    """The pool a write-side tool could reach, narrowed one declared condition at a time."""
    f = [r for r in rs if r["model"] == model and r["verdict"] == "FAILURE"]
    steps = [("failures judged", f)]
    ctrl = [r for r in f if r["control"] == "controllable"]
    ctrl_mixed = [r for r in f if r["control"] in ("controllable", "mixed")]
    steps.append(("... the agent could have controlled (label `controllable`)", ctrl))
    done = [r for r in ctrl if r["end"] == "claims_done"]
    steps.append(("... AND ended with the agent believing it was done", done))
    app = [r for r in done if r["app"] in WRITE_TOOL_APPS]
    steps.append((f"... AND on an application a write tool would support "
                  f"({'/'.join(sorted(WRITE_TOOL_APPS))})", app))
    kind = [r for r in app if r["kind"] in ADDRESSABLE_KINDS]
    steps.append(("... AND with an artefact difference of an addressable kind", kind))

    print(f"\n{model}: the funnel, run by run (task count in brackets)\n")
    base = len(f)
    for label, sel in steps:
        n, t = len(sel), len({r["task8"] for r in sel})
        print(f"  {n:>4} ({t:>3} task)  {100*n/max(1,base):>5.1f}%   {label}")
    print(f"\n  upper bound if every `mixed` label is also reachable: "
          f"{len(ctrl_mixed)} runs ({len({r['task8'] for r in ctrl_mixed})} task), "
          f"{100*len(ctrl_mixed)/max(1,base):.1f}%")
    return {"failures": f, "controllable": ctrl, "claims_done": done, "app": app, "final": kind}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rs = rows()
    print(f"intersection — {len(rs)} judged runs on the pkgfix3 trees "
          f"({sum(1 for r in rs if r['model'] == 'astra')} astra, "
          f"{sum(1 for r in rs if r['model'] == 'sonnet')} sonnet)")
    print("\nThe three ledgers quoted side by side, on the SAME rows, to show they are not the "
          "same denominator:")
    for model in ("astra", "sonnet"):
        f = [r for r in rs if r["model"] == model and r["verdict"] == "FAILURE"]
        lab = sum(1 for r in f if r["control"] != "unlabelled")
        pair = sum(1 for r in f if r["artefact"] == "pair")
        print(f"  {model:7} failures={len(f):>4}  with a controllability label={lab:>4}  "
              f"with a result+gold artefact pair={pair:>4}  "
              f"with NO artefact kept={sum(1 for r in f if r['artefact'] == 'none'):>4}")

    out = {}
    for model in ("astra", "sonnet"):
        out[model] = _funnel(rs, model)

    # --- guard 1: is the difference class diagnostic at all? ------------------------------------
    print("\n\nIs an artefact difference class diagnostic? The same classification on runs the "
          "checker PASSED:\n")
    print(f"  {'model':7} {'verdict':8} {'pairs':>6} " +
          " ".join(k[:9].rjust(10) for k in ("shape", "content", "formatting",
                                             "identical_rendering", "structural")))
    for model in ("astra", "sonnet"):
        for v in ("FAILURE", "SUCCESS"):
            sel = [r for r in rs if r["model"] == model and r["verdict"] == v
                   and r["artefact"] == "pair" and r["kind"]]
            c = Counter(r["kind"] for r in sel)
            print(f"  {model:7} {v:8} {len(sel):>6} " +
                  " ".join(str(c[k]).rjust(10) for k in ("shape", "content", "formatting",
                                                         "identical_rendering", "structural")))
    print("\n  A class that is common on SUCCESS runs is not evidence of a defect: it is what a "
          "correct\n  run looks like to a renderer the checker does not use.")

    # --- guard 2: where the failures actually are, by application -------------------------------
    print("\n\nAstra failures by application and end-of-run class "
          "(the two populations are not the same tasks):\n")
    f = [r for r in rs if r["model"] == "astra" and r["verdict"] == "FAILURE"]
    apps = [a for a, _ in Counter(r["app"] for r in f).most_common()]
    classes = ("claims_done", "declares_fail", "step_cap_admitted", "cli_aborted", "no_answer")
    print(f"  {'app':22} {'fail':>5} " + " ".join(c[:13].rjust(14) for c in classes)
          + f" {'controllable':>13}")
    for app in apps:
        sel = [r for r in f if r["app"] == app]
        c = Counter(r["end"] for r in sel)
        ctrl = sum(1 for r in sel if r["control"] == "controllable")
        print(f"  {app:22} {len(sel):>5} "
              + " ".join(str(c[k]).rjust(14) for k in classes) + f" {ctrl:>13}")

    # --- guard 3: the end class as a trigger, checked on successes ------------------------------
    print("\n\nAstra: `claims_done` is the end of EVERY successful run too, so it cannot be a "
          "trigger by itself:")
    for v in ("SUCCESS", "FAILURE"):
        sel = [r for r in rs if r["model"] == "astra" and r["verdict"] == v]
        c = Counter(r["end"] for r in sel)
        print(f"  {v:8} n={len(sel):>4} " + "  ".join(f"{k}={c[k]}" for k in classes))

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "intersection.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    print(f"\nper-run table: {outdir / 'intersection.csv'} ({len(rs)} rows)")


if __name__ == "__main__":
    main()
