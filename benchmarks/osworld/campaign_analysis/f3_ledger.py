"""F3 (EXPERIMENTS.md X15): the ledger of what the score cannot see. Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/f3_ledger.py [--out tables]

A pass rate on OSWorld-Verified is not a measurement of the agent alone. X11 labelled every task of
the Astra failure analysis controllable or uncontrollable; F1 adjudicated 30 runs by hand and
confirmed two checker errors. F3 puts that next to every arm result, so a difference between two
arms is never read as a capability difference without saying how much of it the agent could not
have touched.

Three columns the headline pass rate hides:

  uncontrollable share -- failures on tasks where the environment, the checker or the wording made
                          the outcome unavailable to any agent (X11 labels);
  G17 share            -- runs on the seven tasks a null agent passes, where a SUCCESS shows no
                          capability and a FAILURE means the agent disturbed a winning initial
                          state;
  fractional rewards   -- runs the binary verdict calls FAILURE while the official reward is above
                          zero (osworld_eval.reward_to_verdict), which `report.py` keeps separate.

A run counts iff eval.json carries a verdict; infra_error.json is a log of attempts and excludes
nothing.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"
LABELS = HERE / "tables" / "e3_controllability.csv"
LABELS_EXTRA = HERE / "tables" / "e3_controllability_sonnet_extra.csv"
ADJ = HERE / "tables" / "f1_adjudication.json"

# X6/G17: a null agent passes these seven. Reported separately in every analysis.
G17 = {"b6781586", "28cc3b7e", "99146c54", "3299584d", "4d117223", "93eabf48", "9219480b"}


def labels():
    out = {}
    for path in (LABELS, LABELS_EXTRA):
        if not path.exists():
            continue
        with path.open() as fh:
            for r in csv.DictReader(fh):
                lab = (r.get("label") or "").strip()
                if r.get("task") and lab:
                    out.setdefault(r["task"][:8], lab)
    return out


def hand_confirmed_checker_errors():
    """Tasks where F1's hand adjudication confirmed the checker itself was wrong."""
    if not ADJ.exists():
        return {}
    out = {}
    for r in json.loads(ADJ.read_text())["runs"]:
        if r["outcome"] in ("checker_weakness_confirmed", "checker_false_negative_confirmed"):
            out[r["task8"]] = r["evidence"][:120]
    return out


def rows():
    sets = json.loads(SETS.read_text())["sets"]
    setof = {t: n for n, s in sets.items() if n != "population361" for t in s["tasks"]}
    lab = labels()
    out = []
    for p in RESULTS.glob("agent_computer_*_official/*/run_*/eval.json"):
        tree = p.parents[2].name
        if "pkgfix3" not in tree:
            continue
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else None)
        if model is None:
            continue
        try:
            e = json.loads(p.read_text())
        except ValueError:
            continue
        if e.get("verdict") not in ("SUCCESS", "FAILURE"):
            continue
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("_official", "").strip("_") or "a0")
        task = p.parents[1].name
        reward = e.get("reward")
        out.append({
            "model": model, "arm": arm, "set": setof.get(task, "?"), "task8": task[:8],
            "verdict": e["verdict"], "label": lab.get(task[:8], "unlabelled"),
            "g17": task[:8] in G17,
            "fractional": bool(e["verdict"] == "FAILURE" and isinstance(reward, (int, float))
                               and 0 < reward < 1),
            "tool_surface_violation": bool(e.get("tool_surface_violation")),
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rs = rows()
    checker_bad = hand_confirmed_checker_errors()
    print(f"F3 — {len(rs)} judged runs on the pkgfix3 trees\n")
    print("Every pass rate with what it cannot see. 'uncontrollable' and 'mixed' are X11 task "
          "labels;\n'G17' are runs on the seven tasks a null agent passes; 'checker' are runs on "
          "tasks where F1\nconfirmed the checker itself is wrong.\n")

    hdr = (f"{'model':7} {'arm':9} {'set':12} {'runs':>5} {'pass':>7} "
           f"{'failed':>7} {'uncontr':>8} {'mixed':>6} {'unlab':>6} {'G17':>5} "
           f"{'checker':>8} {'frac':>5} {'viol':>5}")
    print(hdr); print("-" * len(hdr))
    agg = defaultdict(list)
    for r in rs:
        agg[(r["model"], r["arm"], r["set"])].append(r)
    for key in sorted(agg):
        g = agg[key]
        f = [x for x in g if x["verdict"] == "FAILURE"]
        if len(g) < 5:
            continue
        unc = sum(1 for x in f if x["label"] == "uncontrollable")
        mix = sum(1 for x in f if x["label"] == "mixed")
        unl = sum(1 for x in f if x["label"] == "unlabelled")
        print(f"{key[0]:7} {key[1]:9} {key[2]:12} {len(g):>5} "
              f"{100*sum(1 for x in g if x['verdict']=='SUCCESS')/len(g):>6.1f}% {len(f):>7} "
              f"{unc:>8} {mix:>6} {unl:>6} {sum(1 for x in g if x['g17']):>5} "
              f"{sum(1 for x in g if x['task8'] in checker_bad):>8} "
              f"{sum(1 for x in g if x['fractional']):>5} "
              f"{sum(1 for x in g if x['tool_surface_violation']):>5}")

    print("\nPer model, over every judged pkgfix3 run:")
    for model in ("astra", "sonnet"):
        g = [x for x in rs if x["model"] == model]
        f = [x for x in g if x["verdict"] == "FAILURE"]
        if not g:
            continue
        unc = sum(1 for x in f if x["label"] in ("uncontrollable", "mixed"))
        print(f"  {model}: {len(g)} runs, {len(f)} failed. Of the failures, "
              f"{unc} ({100*unc/max(1,len(f)):.0f}%) are on tasks X11 labels uncontrollable or "
              f"mixed, {sum(1 for x in f if x['label']=='unlabelled')} are on unlabelled tasks.")
        print(f"     runs on G17 tasks: {sum(1 for x in g if x['g17'])}; "
              f"on tasks with a confirmed checker error: "
              f"{sum(1 for x in g if x['task8'] in checker_bad)}; "
              f"failures with a fractional reward: {sum(1 for x in g if x['fractional'])}")

    if checker_bad:
        print("\nTasks where F1 confirmed the checker itself is wrong (every run on them is "
              "uninterpretable as capability):")
        for t, why in sorted(checker_bad.items()):
            print(f"  {t}: {why}")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "f3_ledger.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    print(f"\nper-run table: {outdir / 'f3_ledger.csv'}")


if __name__ == "__main__":
    main()
