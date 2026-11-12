"""DEV1: the first diagnostic tranche, frozen before any of its runs exist. Zero quota to build.

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.dev1_register [--write]

This is a register, not a campaign: it fixes WHICH tasks the first diagnostic screen may touch,
WHY each one was chosen, and what leaves the confirmatory reserve as a result -- so that none of
those answers can be adjusted after the results are seen. It is deliberately separate from
`x22_campaign_lock.json`: X22 stays on its own lock, with its comparisons, analyses, inclusion
criteria, gates and decision rules unchanged, and nothing measured here may re-interpret its
primary results.

Two overlapping classifications, which the exposure manifest reports separately and which the
first version of this plan conflated:

  design_input   the broad, conservative notion: in a Phase 3 set, OR named in a failure analysis
                 or the bug register, OR read run by run, OR a survivor of the intersection
                 funnel. 228 of 361. Its purpose is the honest statement that no intact holdout
                 survives in this population.
  R3             the operational eligibility rule for a NEW intervention: not in a Phase 3 set and
                 never read run by run. 216 of 361. It follows amendment 1's logic -- being named
                 in an analysis is not being tuned on -- applied per decision.

**They are not disjoint: 83 tasks are in both**, every one of them because its id appears in a
failure analysis or the bug register and nothing stronger (`arm_doc` only). R3 is therefore a
**reserve with documented exposure**, never a clean holdout, and a tranche drawn from the
"design_input" side can still consume it. That is exactly what the first draft of this tranche did.

So the pools here are defined against R3 directly:

  pool_free   NOT in R3, and no run directory on this disk. Its exposure is already spent and no
              trajectory survives, so a diagnostic run adds no design exposure. It still costs
              calendar, sandbox time and analysis -- "free" refers to exposure only.
  pool_r3     in R3, no run directory here. Drawing from this pool REMOVES the task from the
              confirmatory reserve, permanently and by declaration.

Selection never looks at a task's past pass rate: the groups are application and pool, and within
a group the order is a seeded shuffle recorded below.
"""
import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "campaign_analysis" / "tables" / "exposure_manifest.csv"
REGISTER = HERE / "dev1_tranche_register.json"
SEED = 20261112

# Approved composition (2026-11-12): a diagnostic tranche aimed at the blind applications, NOT a
# representative sample of the population. vscode has never been run at all; Writer and Thunderbird
# have the least evidence per task; Calc checks whether the three tasks the intersection funnel
# named are representative or exceptions; Impress, Chrome and os are reference groups so the
# diagnosis is not concentrated in one environment.
COMPOSITION = {"vscode": 12, "libreoffice_writer": 10, "libreoffice_calc": 6,
               "libreoffice_impress": 4, "thunderbird": 4, "chrome": 2, "os": 2}

TARGET = {
    "goal": "an increment ON TOP OF e1card2, not an alternative to a0",
    "why": ("the card already covers an identified mechanism; looking for an alternative to a0 "
            "risks rewarding that same recovery again instead of addressing the residual"),
    "diagnosis_arm": "a0, for continuity with the existing ledgers",
    "micro_test_comparison": "e1card2 vs e1card2 + intervention",
    "precondition_before_building": ("on the relevant DEV tasks, verify the problem SURVIVES "
                                     "e1card2 -- never design against a residual the card has "
                                     "already removed"),
    "runs_per_task_diagnostic": 1,
    "runs_per_task_caveat": ("one run per task is triage. It is never reported as an estimate of "
                             "a task's difficulty; targeted replicates separate a recurring "
                             "phenomenon from a single incident."),
}

SECOND_TRANCHE = {
    "opens_only_if": ("the first tranche leaves a SPECIFIC, DOCUMENTED diagnostic uncertainty that "
                      "named additional tasks can resolve"),
    "never_opens_for": "the absence of a winning candidate",
    "cap_tasks": 40,
    "selection_rule": ("same procedure as this register: application and pool groups, seeded "
                       "shuffle, no use of past pass rates; a draw from R3 is declared and "
                       "subtracted from the reserve in the same commit"),
    "requires": "a new register, frozen before its runs, referencing this one by hash",
}

CHECKPOINT = {
    "may_conclude_no_candidate": True,
    "deliverables": [
        "diagnostic coverage: how many runs are reconstructible, and why the others are not",
        "mechanisms supported by evidence, counted per DISTINCT TASK as well as per run",
        "the residual above the card: which problems persist under e1card2",
        "a decision between zero, one or at most two candidates, each with its micro-test and a "
        "stopping rule",
    ],
    "a_fidelity_result_is_a_result": ("if what emerges is mainly a guest, artefact-retrieval or "
                                      "evaluation problem, that legitimately changes the priority "
                                      "and no harness is built"),
}


def _rows():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def _b(v):
    return v == "True"


def in_r3(r):
    """The operational eligibility rule: not in a Phase 3 set, never read run by run."""
    return r["in_set"] == "-" and not _b(r["adjudicated"]) and not _b(r["_funnel"])


def build():
    rows = _rows()
    manifest_hash = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    r3 = [r for r in rows if in_r3(r)]
    design_input = [r for r in rows if _b(r["design_input"])]
    overlap = [r for r in rows if _b(r["design_input"]) and in_r3(r)]

    pool_free = [r for r in rows if not in_r3(r) and r["judged_runs"] == "0"]
    pool_r3 = [r for r in rows if in_r3(r) and r["judged_runs"] == "0"]

    rng = random.Random(SEED)
    picked, from_r3 = [], []
    for app in sorted(COMPOSITION):
        want = COMPOSITION[app]
        free = sorted(x["task"] for x in pool_free if x["app"] == app)
        rng.shuffle(free)
        take_free = free[:want]
        picked += [{"task": t, "app": app, "pool": "free"} for t in take_free]
        short = want - len(take_free)
        if short > 0:
            res = sorted(x["task"] for x in pool_r3 if x["app"] == app)
            rng.shuffle(res)
            take_r3 = res[:short]
            if len(take_r3) < short:
                raise SystemExit(f"{app}: composition asks {want}, only "
                                 f"{len(take_free) + len(take_r3)} tasks available")
            picked += [{"task": t, "app": app, "pool": "r3_declared_draw"} for t in take_r3]
            from_r3 += take_r3

    dev = [p["task"] for p in picked]
    reserve = [r["task"] for r in r3 if r["task"] not in set(from_r3)]

    # --- the checks this register is not allowed to skip ---------------------------------------
    checks = {
        "composition_sums_to_40": len(dev) == sum(COMPOSITION.values()) == 40,
        "no_duplicate_tasks": len(set(dev)) == len(dev),
        "design_input_and_r3_overlap": len(overlap),
        "overlap_is_arm_doc_only": all(
            _b(r["arm_doc"]) and r["in_set"] == "-" and not _b(r["adjudicated"])
            and not _b(r["_funnel"]) for r in overlap),
        "dev_disjoint_from_reserve": not (set(dev) & set(reserve)),
        "r3_draw_declared": sorted(from_r3),
        "reserve_shrinks_by": len(r3) - len(reserve),
        "selection_used_pass_rates": False,
    }
    if not checks["composition_sums_to_40"] or not checks["no_duplicate_tasks"]:
        raise SystemExit(f"register refuses to build: {checks}")
    if not checks["dev_disjoint_from_reserve"]:
        raise SystemExit("register refuses to build: a selected task is still in the reserve")

    reg = {
        "register": "dev1",
        "about": ("first diagnostic tranche, frozen before any of its runs exist. Separate from "
                  "X22, which keeps its own lock unchanged."),
        "fixed_on": "2026-11-12",
        "exposure_manifest_sha256": manifest_hash,
        "x22_lock_sha256": hashlib.sha256(
            (HERE / "x22_campaign_lock.json").read_bytes()).hexdigest(),
        "x22_status": ("unchanged. Its comparisons, analyses, inclusion criteria, gates and "
                       "decision rules stay frozen; no exclusion or primary re-interpretation of "
                       "X22 may be derived from this screen, and any new X22 analysis is labelled "
                       "exploratory."),
        "eligible_universe": {
            "population": len(rows),
            "design_input": len(design_input),
            "r3_reserve_before": len(r3),
            "overlap_design_input_and_r3": len(overlap),
            "overlap_reason": "named in a failure analysis or the bug register, nothing stronger",
            "r3_definition": "not in a Phase 3 set, and never read run by run",
            "r3_honest_name": ("evaluation reserve with documented exposure -- NOT unexplored "
                               "tasks: aggregate rates, per-application counts and power analyses "
                               "have informed decisions, including the choice of R3 itself"),
            "pool_free": len(pool_free),
            "pool_free_definition": ("not in R3 and no run directory on this disk: exposure "
                                     "already spent, no surviving trajectory, so a diagnostic run "
                                     "adds no design exposure -- it still costs calendar, sandbox "
                                     "and analysis"),
            "pool_r3": len(pool_r3),
            "pool_free_by_app": dict(Counter(r["app"] for r in pool_free).most_common()),
            "pool_r3_by_app": dict(Counter(r["app"] for r in pool_r3).most_common()),
        },
        "selection": {
            "criteria": ["application", "pool (free before reserve)"],
            "excluded_criteria": ["past pass rate", "past failure count", "difficulty"],
            "procedure": ("per application: take from pool_free first, in a seeded shuffle; draw "
                          "the shortfall from pool_r3 in a seeded shuffle and declare it"),
            "seed": SEED,
            "composition": dict(COMPOSITION),
        },
        "tasks": picked,
        "tasks_by_app_and_pool": {
            app: dict(Counter(p["pool"] for p in picked if p["app"] == app))
            for app in sorted(COMPOSITION)},
        "r3_declared_draw": sorted(from_r3),
        "reserve_after": {"tasks": len(reserve), "sha256": hashlib.sha256(
            json.dumps(sorted(reserve)).encode()).hexdigest()},
        "reserve_after_ids": sorted(reserve),
        "target": TARGET,
        "second_tranche": SECOND_TRANCHE,
        "checkpoint": CHECKPOINT,
        "checks": checks,
        "run_authorisation": "NOT granted by this register; it is a separate decision",
    }
    reg["register_sha256"] = hashlib.sha256(
        json.dumps(reg, sort_keys=True).encode()).hexdigest()
    return reg


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    reg = build()
    u = reg["eligible_universe"]
    print(f"DEV1 register — {len(reg['tasks'])} tasks, seed {SEED}\n")
    print(f"  population {u['population']}  design_input {u['design_input']}  "
          f"R3 {u['r3_reserve_before']}")
    print(f"  OVERLAP design_input ∩ R3 = {u['overlap_design_input_and_r3']} "
          f"({u['overlap_reason']})")
    print(f"  pool outside R3 with no trajectory: {u['pool_free']}   "
          f"pool inside R3 with no trajectory: {u['pool_r3']}\n")
    print(f"  {'app':22} {'free':>5} {'from R3':>8}")
    for app in sorted(COMPOSITION):
        d = reg["tasks_by_app_and_pool"][app]
        print(f"  {app:22} {d.get('free', 0):>5} {d.get('r3_declared_draw', 0):>8}")
    print(f"\n  declared draw from the reserve: {len(reg['r3_declared_draw'])} tasks")
    print(f"  confirmatory reserve: {u['r3_reserve_before']} -> {reg['reserve_after']['tasks']}")
    print("\n  checks:")
    for k, v in reg["checks"].items():
        shown = v if not isinstance(v, list) else f"{len(v)} ids"
        print(f"    {k:34} {shown}")
    print(f"\n  register sha256 {reg['register_sha256'][:16]}…")
    if a.write:
        REGISTER.write_text(json.dumps(reg, indent=2) + "\n")
        print(f"\n  written {REGISTER}")
    else:
        print("\n  (dry run: pass --write to freeze it)")


if __name__ == "__main__":
    main()
