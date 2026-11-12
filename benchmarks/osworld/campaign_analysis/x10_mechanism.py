"""X10 mechanism counts, outstanding since 3/11. Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/x10_mechanism.py [--out tables]

X10 pre-registered two mechanism checks and X11 recorded them as "not counted yet on the agents'
actions":

  1. is the terminal settings route still used on the settings tasks? (gsettings/dconf, counted on
     the agent's OWN actions, A0 vs E1 vs e1card2)
  2. on `5ca86c6f` and `2bd59342`, the two infeasible tasks where the E1 card produced false DONEs,
     do runs still install or download software? That is the behaviour the added sentence of
     e1card2 forbids, and the reason X10 exists.

"On the agent's own actions" is the whole point and the trap X5 names: under a card arm the system
prompt itself contains the word `gsettings`, so counting over the transcript would count the prompt
instead of the behaviour. `transcripts.typed_text` returns only what the agent typed into the
desktop plus the shell commands it ran.
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transcripts import typed_text  # noqa: E402

RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"

TERMINAL_ROUTE = re.compile(r"\b(gsettings|dconf)\b")
# What the e1card2 sentence forbids once a capability is confirmed missing: working around it by
# installing or downloading software, or building a substitute.
WORKAROUND = re.compile(
    r"\b(apt(-get)?\s+install|pip3?\s+install|snap\s+install|flatpak\s+install|dpkg\s+-i)\b"
    r"|\b(wget|curl)\b.*\bhttps?://", re.I)
REGRESSION_TASKS = ("5ca86c6f", "2bd59342")

ARMS = ("a0", "e1card", "e1card2", "code", "e1b2")
TREES = {"astra": "agent_computer_gpt6astra_max_codex01534_pkgfix3",
         "sonnet": "agent_computer_sonnet5_effortmax_pkgfix3"}


def runs(model, arm, tasks):
    tree = TREES[model] + ("" if arm == "a0" else f"_{arm}") + "_official"
    for t in tasks:
        for d in sorted((RESULTS / tree).glob(f"{t}*/run_*")):
            ev = d / "eval.json"
            if not ev.exists():
                continue
            try:
                v = json.loads(ev.read_text()).get("verdict")
            except ValueError:
                continue
            if v not in ("SUCCESS", "FAILURE"):
                continue
            yield t, d, v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    sets = json.loads(SETS.read_text())["sets"]
    classF = [t[:8] for t in sets["classF"]["tasks"]]
    infeasible = [t[:8] for t in sets["infeasible"]["tasks"]]
    rows = []

    print("X10 mechanism 1 — the terminal settings route, counted on the agent's own actions\n")
    for model in ("astra", "sonnet"):
        per = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))  # arm -> task -> [n, route, pass]
        for arm in ("a0", "e1card", "e1card2", "code"):
            for t, d, v in runs(model, arm, classF):
                texts = typed_text(d)
                used = any(TERMINAL_ROUTE.search(x) for x in texts)
                cell = per[arm][t]
                cell[0] += 1; cell[1] += used; cell[2] += (v == "SUCCESS")
                rows.append({"model": model, "arm": arm, "set": "classF", "task": t,
                             "run": d.name, "verdict": v, "terminal_route": int(used),
                             "workaround": int(any(WORKAROUND.search(x) for x in texts))})
        arms_present = [x for x in ("a0", "e1card", "e1card2", "code") if per[x]]
        if not arms_present:
            continue
        print(f"  {model}: runs using gsettings/dconf, out of judged runs (pass rate in brackets)")
        print(f"    {'task':10} " + " ".join(f"{x:>16}" for x in arms_present))
        for t in classF:
            cells = []
            for arm in arms_present:
                n, r, p = per[arm][t]
                cells.append(f"{r}/{n} ({p}/{n})".rjust(16) if n else "-".rjust(16))
            print(f"    {t:10} " + " ".join(cells))
        tot = []
        for arm in arms_present:
            n = sum(per[arm][t][0] for t in classF); r = sum(per[arm][t][1] for t in classF)
            tot.append(f"{r}/{n}".rjust(16))
        print(f"    {'TOTAL':10} " + " ".join(tot) + "\n")

    print("\nX10 mechanism 2 — workarounds on the two tasks where the E1 card caused false DONEs\n")
    print(f"  {'model':7} {'arm':9} {'task':10} {'runs':>5} {'install/download':>17} "
          f"{'correct refusals':>17}")
    for model in ("astra", "sonnet"):
        for arm in ("a0", "e1card", "e1card2"):
            for t in REGRESSION_TASKS:
                got = list(runs(model, arm, [t]))
                if not got:
                    continue
                w = sum(1 for _, d, _ in got if any(WORKAROUND.search(x) for x in typed_text(d)))
                ok = sum(1 for _, _, v in got if v == "SUCCESS")
                print(f"  {model:7} {arm:9} {t:10} {len(got):>5} {w:>17} {ok:>17}")
                rows += [{"model": model, "arm": arm, "set": "infeasible", "task": t,
                          "run": d.name, "verdict": v,
                          "terminal_route": int(any(TERMINAL_ROUTE.search(x)
                                                    for x in typed_text(d))),
                          "workaround": int(any(WORKAROUND.search(x) for x in typed_text(d)))}
                         for _, d, v in got]
    print("\n  (on an infeasible-by-design task the correct outcome is a refusal, which the "
          "official\n   evaluator scores SUCCESS: the last column is correct refusals, not solves.)")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "x10_mechanism.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"\nper-run table: {outdir / 'x10_mechanism.csv'} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
