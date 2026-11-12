"""F1, the 124 sampled runs with no E3 verdict: a worklist ordered by closeness to the gold.

    .venv/bin/python benchmarks/osworld/campaign_analysis/f1_rank_untriaged.py [--out tables]

The first F1 batch was triaged by E3 disagreement. 124 runs of the frozen sample carry no E3
verdict at all, because the X13 E3 sample (187 runs) and the F1 sample (240) were drawn by
different rules, so there is no disagreement to triage and the pre-registration says they need
direct adjudication.

Adjudicating 124 runs by hand is a multi-session job, so this orders them by where a checker error
can actually hide. The ordering is mechanical, not a judgement:

  a FAILURE whose saved artefacts are nearly identical to the gold is a checker-strictness
  candidate -- the agent produced almost exactly the expected state and was scored 0;

  a SUCCESS whose artefacts differ widely from the gold is a leniency candidate -- the checker
  accepted a state far from what it was built to accept.

Distance is the unified-diff length between the renderings of the result and gold files the
evaluator kept, the same measure f1_dossier shows. It is a *pointer*, never a verdict: F1's first
batch showed a 296-line diff on two runs that correctly PASS (formula differences `sheet_data` does
not look at), which is exactly why this file ranks rather than decides.
"""
import argparse
import csv
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E  # noqa: E402

TABLES = HERE / "tables"
SAMPLE = TABLES / "f1_sample.json"
E3B = HERE.parent / "results" / "_e3" / "verdicts_b"


def has_e3(model, task, verdict):
    return (E3B / f"{model}_{task}_{verdict}.json").exists()


def gold_pairs(run_dir):
    """(result, gold) artefact pairs the evaluator kept, matched by order within each kind."""
    try:
        ev = json.loads((Path(run_dir) / "eval.json").read_text())
    except (OSError, ValueError):
        return []
    arts = [a for a in ev.get("eval_artifacts", []) if a.get("kept")]
    res = [a["name"] for a in arts if not a.get("is_gold")]
    gold = [a["name"] for a in arts if a.get("is_gold")]
    return list(zip(res, gold))


def distance(run_dir):
    """Total unified-diff lines between result and gold renderings; None when there is no pair."""
    d = Path(run_dir) / "eval_artifacts"
    pairs = gold_pairs(run_dir)
    if not pairs:
        return None
    total = 0
    for r, g in pairs:
        if not (d / r).is_file() or not (d / g).is_file():
            continue
        a = E.render_file(d / r).splitlines()
        b = E.render_file(d / g).splitlines()
        total += sum(1 for x in difflib.unified_diff(a, b, n=0, lineterm="")
                     if not x.startswith("@@"))
    return total


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TABLES))
    a = ap.parse_args(argv)
    sample = json.loads(SAMPLE.read_text())["runs"]
    rows = []
    for r in sample:
        if has_e3(r["model"], r["task"], r["verdict"]):
            continue
        run = Path(r["run"])
        if not run.is_dir():
            rows.append({**{k: r[k] for k in ("model", "image", "bucket", "task", "verdict")},
                         "task8": r["task"][:8], "run": r["run"], "distance": None,
                         "pairs": 0, "priority": "unreachable"})
            continue
        dist = distance(run)
        pairs = len(gold_pairs(run))
        if dist is None:
            pri = "no gold pair (adjudicate from the instruction and the screenshot)"
        elif r["verdict"] == "FAILURE":
            pri = ("near-gold FAILURE: checker-strictness candidate" if dist <= 20
                   else "FAILURE, far from gold")
        else:
            pri = ("SUCCESS far from gold: leniency candidate" if dist >= 200
                   else "SUCCESS near gold")
        rows.append({**{k: r[k] for k in ("model", "image", "bucket", "task", "verdict")},
                     "task8": r["task"][:8], "run": r["run"], "distance": dist,
                     "pairs": pairs, "priority": pri})

    order = {"near-gold FAILURE: checker-strictness candidate": 0,
             "SUCCESS far from gold: leniency candidate": 1,
             "no gold pair (adjudicate from the instruction and the screenshot)": 2,
             "FAILURE, far from gold": 3, "SUCCESS near gold": 4, "unreachable": 5}
    rows.sort(key=lambda x: (order.get(x["priority"], 9),
                             x["distance"] if x["distance"] is not None else 10 ** 9))

    print(f"F1 — {len(rows)} sampled runs with no E3 verdict\n")
    from collections import Counter
    for k, v in sorted(Counter(x["priority"] for x in rows).items(), key=lambda kv: order.get(kv[0], 9)):
        print(f"  {v:>4}  {k}")

    head = [x for x in rows if x["priority"].startswith(("near-gold", "SUCCESS far"))]
    print(f"\nWorklist head — {len(head)} runs where a checker error can hide:")
    print(f"  {'model':7} {'image':9} {'bucket':20} {'task':9} {'verdict':8} {'diff':>6}")
    for x in head[:30]:
        print(f"  {x['model']:7} {x['image']:9} {x['bucket']:20} {x['task8']:9} "
              f"{x['verdict']:8} {x['distance']:>6}")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "f1_untriaged.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"\nworklist: {outdir / 'f1_untriaged.csv'}")


if __name__ == "__main__":
    main()
