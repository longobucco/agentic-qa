"""V2 (EXPERIMENTS.md X15): calibrate the E3 (b) offline verifier against F1's human labels.

    .venv/bin/python benchmarks/osworld/campaign_analysis/v2_calibrate.py [--out tables]

X13 measured E3's *precision against the official checker* (90.5% for variant b). That is agreement
with an instrument, not accuracy: where the checker is itself wrong, agreeing with it is an error.
The meta-evaluation Mis-Score and OpenComputer require is this one — E3 against human adjudication —
and it only exists once F1 has labels.

The question has a sharp form. On a run where E3 and the checker DISAGREE, exactly one of them is
right, and F1's adjudication says which:

    E3 right  <=> the human found the checker wrong   (checker_weakness_confirmed, or a confirmed
                                                       checker false negative)
    E3 wrong  <=> the human found the checker correct  (checker_correct, checker_correct_e3_error)

Two outcome classes are not adjudicable this way and are reported separately rather than folded in:
`not_determinable` (the saved evidence does not settle it) and the broken-task classes, where the
checker is correct *given its definition* but the task cannot be measured on this guest, so "who is
right" is not the right question.

The split that carries the finding: whether E3 **had an evidence channel** for what the checker
reads. A run has one when the evaluator saved result artefacts for E3 to inspect
(`eval.json.eval_artifacts`); a settings, active-tab or shape-fill checker leaves none, and there
E3's verdict rests on the screenshot and the agent's own claim.
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLES = HERE / "tables"
ADJ = TABLES / "f1_adjudication.json"
TRIAGE = TABLES / "f1_triage.csv"

E3_RIGHT = {"checker_weakness_confirmed", "checker_false_negative_confirmed"}
E3_WRONG = {"checker_correct", "checker_correct_e3_error"}
BROKEN_TASK = {"task_unmeasurable_on_this_guest", "task_impossible_refusal_scored_failure"}


def evidence_channel(run_dir):
    """Did the evaluator save artefacts E3 could inspect? ([] or missing = no channel.)"""
    ev = Path(run_dir) / "eval.json"
    if not ev.exists():
        return None
    try:
        arts = json.loads(ev.read_text()).get("eval_artifacts")
    except ValueError:
        return None
    return bool(arts)


def has_v1(run_dir):
    return (Path(run_dir) / "final_state" / "manifest.json").exists()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TABLES))
    a = ap.parse_args(argv)
    adj = json.loads(ADJ.read_text())
    runs = adj["runs"]
    for r in runs:
        r["evidence_channel"] = evidence_channel(r["run"])
        r["v1_capture"] = has_v1(r["run"])

    print(f"V2 — E3 (b) against {len(runs)} hand-adjudicated disagreements "
          f"(F1 first batch, {adj['adjudicated_on']})\n")

    adjudicable = [r for r in runs if r["outcome"] in E3_RIGHT | E3_WRONG]
    right = [r for r in adjudicable if r["outcome"] in E3_RIGHT]
    broken = [r for r in runs if r["outcome"] in BROKEN_TASK]
    undet = [r for r in runs if r["outcome"] == "not_determinable"]

    print("Headline. On a disagreement, exactly one of E3 and the checker is right:")
    print(f"  adjudicable disagreements      {len(adjudicable):>3}")
    print(f"  E3 right (checker was wrong)   {len(right):>3}")
    print(f"  E3 wrong (checker was right)   {len(adjudicable) - len(right):>3}")
    print(f"  **E3 precision when it disagrees with the checker: "
          f"{len(right)}/{len(adjudicable)} = {100*len(right)/max(1,len(adjudicable)):.1f}%**")
    print(f"  reported separately: broken task {len(broken)}, not determinable {len(undet)}")

    print("\nBy evidence channel — the pre-registered split:")
    hdr = f"  {'channel':26} {'n':>3} {'E3 right':>9} {'E3 wrong':>9} {'precision':>10}"
    print(hdr)
    for label, want in (("artefacts saved", True), ("no artefacts", False), ("unknown", None)):
        g = [r for r in adjudicable if r["evidence_channel"] is want]
        if not g:
            continue
        rt = sum(1 for r in g if r["outcome"] in E3_RIGHT)
        print(f"  {label:26} {len(g):>3} {rt:>9} {len(g)-rt:>9} "
              f"{100*rt/len(g):>9.1f}%")

    print("\nBy guest image — the archive stratum became reachable on 12/11:")
    for img in ("pkgfix3", "original"):
        g = [r for r in adjudicable if r.get("image") == img]
        if not g:
            continue
        rt = sum(1 for r in g if r["outcome"] in E3_RIGHT)
        print(f"  {img:12} n={len(g):>3}  E3 right {rt:>2}  precision {100*rt/len(g):>5.1f}%")

    print("\nBy direction of the disagreement:")
    for d in ("false_negative_candidate", "false_positive_candidate"):
        g = [r for r in adjudicable if r["triage"] == d]
        if not g:
            continue
        rt = sum(1 for r in g if r["outcome"] in E3_RIGHT)
        print(f"  {d:28} n={len(g):>3}  E3 right {rt:>2}  precision {100*rt/len(g):>5.1f}%")

    print("\nAttributed cause of every adjudicated run:")
    for k, v in sorted(Counter(r["attributed_cause"] for r in runs).items(), key=lambda kv: -kv[1]):
        print(f"  {k:26} {v:>3}")

    print(f"\nV1 final-state capture present: "
          f"{sum(1 for r in runs if r['v1_capture'])} of {len(runs)} runs "
          "(the capture postdates these runs, which is why two are not determinable)")

    print("""
Reading.
  E3's 90.5% in X13 is precision against the official checker. This is the first measurement of its
  precision against a human, and only on the subset where the two instruments disagree -- the
  hardest subset by construction, and the only one where the question has an answer. The number is
  not comparable with X13's and does not replace it: it bounds how much weight a disagreement can
  carry, which is exactly what a gate or a selector would have relied on.

  The pre-registered prediction is NOT supported, and the direction is the opposite of the one
  written down in the F1 sheet. The prediction was that E3 would be much more precise where it has
  saved artefacts to inspect and near chance where it does not. Measured: 0/11 with artefacts and
  1/6 without. With n of 11 and 6 the difference carries no weight on its own (a Fisher exact test
  on 0/11 against 1/6 is far from significant), so the defensible statement is the flat one: E3 is
  wrong on 16 of 17 adjudicable disagreements, whether or not artefacts were saved.

  The artefact-bearing cases say why, and it is not a missing channel. E3 HAD the files and still
  judged wrong, because its rendering of them is not aligned with what the checker compares: for
  Calc it reads formulas where `sheet_data` compares internal values through DataFrame.equals, and
  for pptx it renders text where `compare_pptx_files` compares fills and geometry with strict RGB.
  So the corrective is not only V1 capturing more state -- it is making the verifier's view of an
  artefact match the checker's predicate. That is a different and cheaper fix, and it is testable
  offline on these same runs.

  Consequence for A4 and X17, unchanged in direction and now quantified: a disagreement from E3 is
  not evidence of a checker error. Reconsidering either needs the rendering aligned first, then
  this precision re-measured.""")

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    out = {
        "about": ("V2: E3 (b) calibrated against F1's human labels. Precision is measured only on "
                  "runs where E3 and the official checker disagree, because that is where the "
                  "adjudication settles which instrument was right."),
        "calibrated_on": "2026-11-12",
        "n_adjudicated": len(runs), "n_adjudicable": len(adjudicable),
        "e3_right": len(right),
        "e3_precision_on_disagreements": round(len(right) / max(1, len(adjudicable)), 4),
        "excluded": {"broken_task": len(broken), "not_determinable": len(undet)},
        "by_evidence_channel": {
            str(w): {"n": len([r for r in adjudicable if r["evidence_channel"] is w]),
                     "e3_right": len([r for r in adjudicable
                                      if r["evidence_channel"] is w and r["outcome"] in E3_RIGHT])}
            for w in (True, False, None)},
        "v1_capture_present": sum(1 for r in runs if r["v1_capture"]),
        "by_image": {img: {"n": len([r for r in adjudicable if r.get("image") == img]),
                           "e3_right": len([r for r in adjudicable if r.get("image") == img
                                            and r["outcome"] in E3_RIGHT])}
                     for img in ("pkgfix3", "original")},
        "limits": ("30 labels: every disagreement of the frozen F1 sample, 21 on pkgfix3 and 9 on "
                   "the original image. The agreement rows of the triage "
                   "are NOT hand-adjudicated, so this reports precision on disagreements only and "
                   "no overall accuracy -- assuming the checker right wherever E3 agrees with it "
                   "would be assuming the very thing F1 exists to test."),
    }
    (outdir / "v2_calibration.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwritten: {outdir / 'v2_calibration.json'}")


if __name__ == "__main__":
    main()
