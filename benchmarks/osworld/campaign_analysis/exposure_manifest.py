"""Exposure manifest: what each of the 361 tasks has already been used for, and for which
comparison it can still serve as confirmation. Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/exposure_manifest.py [--out tables]

A held-out set is not a property of a task, it is a property of a **(task, design decision)**
pair. The card frozen on 2026-11-12 and a tool designed next week have different exposure
histories, so "out of design" has to be answered once per decision, not once per task. Amendment 1
of the X22 lock settled it for the frozen card: being named in an analysis is not being tuned on,
because the arm text carries no task-specific content (test_arm_text_is_generic). That argument
does **not** transfer to an intervention chosen *because* of what a specific task's runs showed --
which is exactly how a tool candidate would now be chosen (`intersection.py` named three Calc
tasks).

So this file records exposure as evidence rather than as a verdict, one row per task:

  in_set          the Phase 3 set it belongs to, if any -- those carry arm comparisons already
  judged_runs     how many judged runs exist, and on which models: results we have aggregated
  examined        its id appears in a log, audit sheet or analysis table: its runs were read
  arm_doc         its id appears in a failure analysis or the bug register: the documents the
                  cards were written from
  adjudicated     a human read this task's runs one by one (F1)
  design_input    it demonstrably fed a design decision (see `_design_input`)

and then answers eligibility separately for each declared decision. Nothing here re-opens a frozen
stratum: the a0-vs-e1card2 comparison keeps the `out_of_design` definition amendment 1 froze. The
new columns exist for decisions that have not been made yet.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from benchmarks.osworld import tasks as _tasks           # noqa: E402
from benchmarks.osworld import x22_lock as LOCK          # noqa: E402
from intersection import app_of                          # noqa: E402

APP_ORDER = ("libreoffice_calc", "chrome", "libreoffice_impress", "os",
             "libreoffice_writer", "gimp", "vscode", "thunderbird", "vlc",
             "unknown")

RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"
TABLES = HERE / "tables"
ID8 = re.compile(r"\b([0-9a-f]{8})\b")

# Written after the X22 lock was frozen, while choosing where a tool could help. A task named here
# was read *in order to* pick an intervention, which is the strongest form of exposure.
POST_LOCK_DOCS = (
    "docs/2026-11-12_spazio_di_ricerca_harness.md",
    "benchmarks/osworld/campaign_analysis/X12_CODE_ARM_REPORT.md",
    "benchmarks/osworld/campaign_analysis/F1_X22_TRIAGE_SHEET.md",
)


def _ids_in(paths):
    out = set()
    for rel in paths:
        p = ROOT / rel if not Path(rel).is_absolute() else Path(rel)
        if p.is_file():
            out |= set(ID8.findall(p.read_text(errors="replace")))
    return out


# Tables that enumerate the whole population rather than recording what was examined. Counting
# these as exposure marks all 361 tasks and makes the manifest say nothing -- which is exactly
# what the first run of this file did.
POPULATION_TABLES = {"task_matrix.csv", "exposure_manifest.csv"}


def _ids_in_tables():
    """Task ids named in a per-run analysis ledger: tables built from runs that were read, not
    tables that list the population."""
    out = set()
    for p in sorted(TABLES.rglob("*")):
        if (p.is_file() and p.suffix in (".csv", ".json", ".txt")
                and p.name not in POPULATION_TABLES):
            out |= set(ID8.findall(p.read_text(errors="replace")))
    return out


def _main_campaign():
    """Per-task coverage of the campaigns that ran BEFORE the pkgfix3 trees, from the record that
    survived them (`tables/task_matrix.csv`). Those results are not on this disk any more, so the
    matrix is the authority: a task with a pass/valid cell there was run and its result entered
    the analyses the cards were written from.
    """
    out = {}
    m = TABLES / "task_matrix.csv"
    if not m.exists():
        return out
    with m.open() as fh:
        for r in csv.DictReader(fh):
            cols = [k for k in r if k.endswith("_pass/valid")]
            seen = [k.replace("_pass/valid", "") for k in cols if (r[k] or "").strip()]
            if r.get("task_id"):
                out[r["task_id"]] = seen
    return out


def _judged_runs():
    by = defaultdict(Counter)
    for p in RESULTS.glob("agent_computer_*_official/*/run_*/eval.json"):
        tree = p.parents[2].name
        if "nullagent" in tree:
            continue
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else None)
        if model is None:
            continue
        try:
            v = json.loads(p.read_text()).get("verdict")
        except ValueError:
            continue
        if v in ("SUCCESS", "FAILURE"):
            by[p.parents[1].name][model] += 1
            by[p.parents[1].name]["pass" if v == "SUCCESS" else "fail"] += 1
    return by


def _design_input(row, funnel_tasks):
    """Did this task feed a design decision? Conservative: any ONE of these is enough.

    - it is in a Phase 3 set: those sets exist to compare arms, and the cards were written while
      reading them;
    - it is named in a failure analysis or the bug register (ARM_DOCS): the documents the card
      text was derived from;
    - a human adjudicated its runs one by one (F1), or it survived the intersection funnel --
      both are "this task's runs chose the hypothesis".
    """
    return bool(row["in_set"] != "-" or row["arm_doc"] or row["adjudicated"]
                or row["task8"] in funnel_tasks)


def rows():
    sets = json.loads(SETS.read_text())["sets"]
    setof = {t: n for n, s in sets.items() if n != "population361" for t in s["tasks"]}
    arm_cited = _ids_in(LOCK.ARM_DOCS)
    log_cited = _ids_in(LOCK.LOG_DOCS)
    post_lock = _ids_in(POST_LOCK_DOCS)
    tabled = _ids_in_tables()
    judged = _judged_runs()

    main = _main_campaign()
    adjudicated = set()
    adj = TABLES / "f1_adjudication.json"
    if adj.exists():
        adjudicated = {r["task8"] for r in json.loads(adj.read_text())["runs"]}

    funnel = set()
    inter = TABLES / "intersection.csv"
    if inter.exists():
        with inter.open() as fh:
            for r in csv.DictReader(fh):
                if (r["model"] == "astra" and r["verdict"] == "FAILURE"
                        and r["control"] == "controllable" and r["end"] == "claims_done"):
                    funnel.add(r["task8"])

    out = []
    for t in _tasks.load_tasks():
        tid, t8 = t["id"], t["id"][:8]
        runs = judged.get(tid, Counter())
        row = {
            "task": tid, "task8": t8, "app": app_of(t),
            "in_set": setof.get(tid, "-"),
            "judged_runs": runs["astra"] + runs["sonnet"],
            "astra_runs": runs["astra"], "sonnet_runs": runs["sonnet"],
            "main_campaign": "|".join(main.get(tid, [])),
            "arm_doc": t8 in arm_cited, "log_doc": t8 in log_cited,
            "post_lock_doc": t8 in post_lock, "in_table": t8 in tabled,
            "adjudicated": t8 in adjudicated, "_funnel": t8 in funnel,
        }
        row["ever_run"] = bool(row["judged_runs"] or row["main_campaign"])
        row["examined"] = bool(row["log_doc"] or row["in_table"] or row["post_lock_doc"])
        row["design_input"] = _design_input(row, funnel)
        out.append(row)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TABLES))
    a = ap.parse_args(argv)
    rs = rows()
    funnel_ids = {r["task8"] for r in rs if r.get("_funnel")}
    n = len(rs)
    print(f"exposure manifest — {n} tasks of the frozen population\n")

    print("Exposure, cumulative and conservative (each line adds the one above):\n")
    never_run = [r for r in rs if not r["ever_run"]]
    run_only = [r for r in rs if r["ever_run"] and not r["examined"]
                and not r["design_input"]]
    examined = [r for r in rs if r["examined"] and not r["design_input"]]
    design = [r for r in rs if r["design_input"]]
    print(f"  (of the 361, {sum(1 for r in rs if not r['ever_run'])} have never been run by "
          f"either model, in any campaign)\n")
    for label, sel in (("never run, never named: untouched", 
                        [r for r in never_run if not r["examined"] and not r["design_input"]]),
                       ("run, but never named anywhere (aggregate only)", run_only),
                       ("examined: named in a log, audit sheet or ledger table", examined),
                       ("design input: fed a card, a tool hypothesis or a hand adjudication",
                        design)):
        print(f"  {len(sel):>4}  {100*len(sel)/n:>5.1f}%   {label}")

    print("\n\nEligibility, per decision. A stratum frozen before a decision keeps its definition.\n")
    frozen = set(json.loads((HERE.parent / 'x22_campaign_lock.json').read_text())
                 ["stage3"]["strata"]["out_of_design"])
    print(f"  (A) a0 vs e1card2, FROZEN 2026-11-12 (lock amendment 1)")
    print(f"      primary stratum `out_of_design`: {len(frozen)} tasks -- unchanged, and nothing "
          f"here reopens it.")
    print(f"      Justification on record: the arm text carries no task-specific content, so a "
          f"task merely\n      described in an analysis is not a task the arm was tuned on.")

    # Three candidate rules for "may still confirm a NEW intervention", from strictest to the one
    # amendment 1 already defends. The cut is a decision, not a calculation: each line says what
    # it costs. `individually_read` is the strong form of exposure -- a human read THIS task's
    # runs one by one, or the task survived the funnel that would pick the intervention.
    for r in rs:
        r["individually_read"] = bool(r["adjudicated"] or r["task8"] in funnel_ids)
    rules = [
        ("R1  never run and never named anywhere",
         lambda r: not r["ever_run"] and not r["examined"] and not r["design_input"]),
        ("R2  never a design input (set, failure analysis, bug register, hand reading)",
         lambda r: not r["design_input"]),
        ("R3  not in a Phase 3 set and never read run by run  [amendment 1's logic, per decision]",
         lambda r: r["in_set"] == "-" and not r["individually_read"]),
    ]
    print("  Three candidate rules for what may still confirm a new intervention:\n")
    for label, rule in rules:
        sel = [r for r in rs if rule(r)]
        apps_ = Counter(r["app"] for r in sel)
        print(f"    {len(sel):>4} task  {label}")
        print("              " + "  ".join(f"{k.replace('libreoffice_', 'lo_')}={apps_[k]}"
                                           for k in APP_ORDER if apps_[k]))
    print("\n    R1 is unusable, and that is the finding: after two campaigns and six analysis "
          "documents\n    there is no untouched holdout left in this population. The choice is "
          "between R2 and R3,\n    and it is a judgement about which exposure actually "
          "contaminates a tool decision --\n    not a number this file can compute.")

    print("\n\nBy application — what a stratified split can actually draw on:\n")
    apps = [a_ for a_, _ in Counter(r["app"] for r in rs).most_common()]
    print(f"  {'app':22} {'pop':>5} {'mai eseguiti':>13} {'eleggibili (B)':>15} "
          f"{'design input':>13} {'run giudicate':>14}")
    for app in apps:
        sel = [r for r in rs if r["app"] == app]
        print(f"  {app:22} {len(sel):>5} "
              f"{sum(1 for r in sel if not r['ever_run']):>13} "
              f"{sum(1 for r in sel if not r['design_input'] and not r['examined']):>15} "
              f"{sum(1 for r in sel if r['design_input']):>13} "
              f"{sum(r['judged_runs'] for r in sel):>14}")

    print("\n\nThe gap the DEV screen would have to close "
          "(applications with the least evidence per task):\n")
    for app in apps:
        sel = [r for r in rs if r["app"] == app]
        runs = sum(r["judged_runs"] for r in sel)
        seen = sum(1 for r in sel if r["ever_run"])
        print(f"  {app:22} {seen:>3}/{len(sel):<3} task con almeno una run giudicata "
              f"({runs:>4} run in tutto)")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "exposure_manifest.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    print(f"\nmanifest: {outdir / 'exposure_manifest.csv'} ({len(rs)} rows)")


if __name__ == "__main__":
    main()
