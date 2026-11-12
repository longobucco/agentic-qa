"""F1 triage (EXPERIMENTS.md X15): order the hand-adjudication worklist by E3 (b) disagreement.

    .venv/bin/python benchmarks/osworld/campaign_analysis/f1_triage.py [--out tables]

The pre-registration says the audit is triaged from E3 (b)'s disagreements with the official
checker, then adjudicated by hand on the evidence. Two directions, and the second is the one
Mis-Score does not cover:

  false_negative_candidate -- official FAILURE, E3 says *done*: the checker may be wrong
  false_positive_candidate -- official SUCCESS, E3 says *not_done*: the checker may be lenient

`agree` and `abstain` rows are kept in the table so the triage's own base rates are visible: a
disagreement rate is only interpretable next to them.

Reachability is reported because the frozen F1 sample (tables/f1_sample.json, seed 20261105) points
147 of its 240 runs at the external archive. Those cannot be adjudicated with the disk unmounted,
and the table says so per row rather than silently dropping them.
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
E3B = RESULTS / "_e3" / "verdicts_b"
SAMPLE = HERE / "tables" / "f1_sample.json"

# E3's own decision rule (X13): done / not_done / cannot_be_checked (an abstention).
CLASSIFY = {
    ("FAILURE", "done"): "false_negative_candidate",
    ("SUCCESS", "not_done"): "false_positive_candidate",
    ("SUCCESS", "done"): "agree",
    ("FAILURE", "not_done"): "agree",
}


def e3_verdicts():
    """{(model, task): [record, ...]} from the frozen E3 (b) verdicts."""
    out = {}
    if not E3B.exists():
        return out
    for p in sorted(E3B.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        out.setdefault((d.get("model"), d.get("task")), []).append(d)
    return out


def rows():
    sample = json.loads(SAMPLE.read_text())["runs"]
    e3 = e3_verdicts()
    out = []
    for r in sample:
        key = (r["model"], r["task"])
        recs = e3.get(key, [])
        # E3's sample is per (model, task, official verdict): match the verdict too.
        rec = next((x for x in recs if x.get("verdict") == r["verdict"]), None)
        e3v = (rec or {}).get("e3")
        klass = ("no_e3_verdict" if e3v is None
                 else "abstain" if e3v in ("cannot_check", "cannot_be_checked")
                 else CLASSIFY.get((r["verdict"], e3v), f"unexpected:{e3v}"))
        run = Path(r["run"])
        out.append({
            "model": r["model"], "image": r["image"], "bucket": r["bucket"],
            "task": r["task"], "task8": r["task"][:8], "official": r["verdict"],
            "e3": e3v or "-", "triage": klass,
            "reachable": run.is_dir(), "run": r["run"],
            "e3_run": (rec or {}).get("run", ""),
        })
    # worklist order: disagreements first, reachable first, then by model and task
    rank = {"false_negative_candidate": 0, "false_positive_candidate": 1,
            "abstain": 2, "agree": 3, "no_e3_verdict": 4}
    out.sort(key=lambda r: (rank.get(r["triage"], 9), not r["reachable"], r["model"], r["task"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rs = rows()
    print(f"F1 triage — {len(rs)} runs of the frozen sample\n")

    by = Counter(r["triage"] for r in rs)
    reach = Counter((r["triage"], r["reachable"]) for r in rs)
    print(f"{'triage':28} {'total':>6} {'reachable':>10} {'on archive':>11}")
    print("-" * 58)
    for k in sorted(by, key=lambda k: -by[k]):
        print(f"{k:28} {by[k]:>6} {reach[(k, True)]:>10} {reach[(k, False)]:>11}")

    print(f"\nreachable now: {sum(1 for r in rs if r['reachable'])} of {len(rs)} "
          f"(the other rows live on the external archive, unmounted)")

    work = [r for r in rs if r["reachable"] and r["triage"].endswith("_candidate")]
    print(f"\nWorklist, reachable disagreements: {len(work)}")
    print(f"{'#':>3} {'model':7} {'bucket':20} {'task':9} {'official':9} {'E3':10} {'direction':26}")
    for i, r in enumerate(work, 1):
        print(f"{i:>3} {r['model']:7} {r['bucket']:20} {r['task8']:9} {r['official']:9} "
              f"{r['e3']:10} {r['triage']:26}")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "f1_triage.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    (outdir / "f1_worklist.json").write_text(json.dumps(work, indent=2) + "\n")
    print(f"\ntriage table: {outdir / 'f1_triage.csv'}")
    print(f"worklist:     {outdir / 'f1_worklist.json'}")


if __name__ == "__main__":
    main()
