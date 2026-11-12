"""Generator of the X22 campaign lock: what the confirmatory campaign is allowed to run and how it
will be analysed, written down before it runs.

    .venv/bin/python -m benchmarks.osworld.x22_lock --write

Committed before being applied, like data/heldoutc_rule.py, so the analysis strata of stage 3
cannot have been chosen on the results. Gate A (gates/contract.py) refuses to launch anything this
lock does not declare; Gate C (gates/sequential.py) reads its look schedule and boundaries.

The three strata of stage 3 are the point of the file. The population screen is run on all 361
tasks, but the *primary* analysis is restricted to the tasks that could not have informed any arm:

  primary_out_of_sample -- in no Phase 3 set, and the 8-hex id prefix appears in none of the
                           documents that wrote the arms (ASTRA_ANALYSIS, ASTRA_IMPROVEMENT_
                           RESEARCH, FINDINGS, BUG_REGISTER, astra_ledger). This is the
                           pre-registered primary.
  never_cited           -- additionally absent from every log and register, i.e. never named
                           anywhere. The strictest sensitivity analysis.
  all361                -- the whole population. The other sensitivity analysis.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from benchmarks.osworld import arms, config, phase3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCK = HERE / "x22_campaign_lock.json"
POPULATION = ROOT / "scripts" / "g_protocol361_ids.txt"

TAG = "phase3-confirmatory-frozen"
SUFFIX = "pkgfix3c"
EVALUATOR_COMMIT = "091f5ef1d5544bc74953c77875d5feb5bed30108"

# Documents that wrote the arms: the failure analysis and the register the card texts were
# distilled from. A task named here could have informed an arm, so it is out of the primary.
ARM_DOCS = (
    "benchmarks/osworld/campaign_analysis/ASTRA_ANALYSIS.md",
    "benchmarks/osworld/campaign_analysis/ASTRA_IMPROVEMENT_RESEARCH.md",
    "benchmarks/osworld/campaign_analysis/FINDINGS.md",
    "benchmarks/osworld/campaign_analysis/BUG_REGISTER.md",
    "benchmarks/osworld/campaign_analysis/astra_ledger.json",
)
# Everything else that merely mentions a task (experiment log, research log, audits, the proposal).
LOG_DOCS = (
    "benchmarks/osworld/campaign_analysis/EXPERIMENTS.md",
    "benchmarks/osworld/campaign_analysis/RESEARCH_LOG.md",
    "benchmarks/osworld/campaign_analysis/SUMMARY.md",
    "benchmarks/osworld/campaign_analysis/METHODOLOGY.md",
    "benchmarks/osworld/campaign_analysis/F1_ASTRA_SHEETS.md",
    "benchmarks/osworld/campaign_analysis/F1_SONNET_STEPCAP_AUDIT.md",
    "docs/2026-11-12_harness_astra_proposta.md",
)
_ID8 = re.compile(r"\b([0-9a-f]{8})\b")

AMENDMENTS = [
    {
        "n": 1, "on": "2026-11-12",
        "made_before": "any X22 run existed and any look boundary was frozen",
        "change": ("the primary stratum of stage 3 becomes `out_of_design` (the population minus "
                   "the tasks of a Phase 3 set, 219 tasks) instead of `primary_out_of_sample` "
                   "(134); the latter and `never_cited` stay as pre-registered sensitivities."),
        "why": ("E7's power calculation came out at zero, and the reason is a selection the first "
                "stratum definition had not accounted for: tasks get cited in a failure analysis "
                "BECAUSE they failed, so excluding cited tasks excludes the failures. Of the 134 "
                "tasks of `primary_out_of_sample` with reference data, 43 of 68 are saturated and "
                "only 37% have any room; of `never_cited`, 24 of 25. Neither can show an effect."),
        "justification": ("excluding a task because an analysis document names it is only "
                          "necessary if the arm text carries task-specific content. It does not: "
                          "checked mechanically on all five arms, none contains a task id or any "
                          "task instruction (test_arm_text_is_generic). The cards were written "
                          "from patterns -- settings are reachable from a terminal; results are "
                          "read from files -- not from facts about individual tasks, so the "
                          "out-of-sample unit is the task family, and a task merely described in "
                          "an analysis is not a task the arm was tuned on. Being in a Phase 3 set "
                          "is different: those tasks carry arm comparisons already."),
        "cost": ("`out_of_design` has 219 tasks, 50% of those with reference data still having "
                 "room, against 37% of the stricter stratum. The stricter strata remain reported, "
                 "so a reader who rejects this argument can read the conservative number."),
    },
    {
        "n": 2, "on": "2026-11-12",
        "made_before": "any X22 run existed; the frozen look boundaries are carried over unchanged",
        "change": ("stage 3's PRIMARY deliverables become the bounded effect estimate and the "
                   "no-harm claim; the exact-McNemar significance test is demoted to a declared "
                   "secondary, stated as underpowered below f = 0.20. No set, test, stratum, look "
                   "schedule or alpha changes."),
        "why": ("gates/power_check.py, run before any X22 run: the design is at its ceiling (2, 3 "
                "and 5 runs per task give power 0.19/0.20/0.19 at f=0.10, so more runs do not "
                "help, and a 33-task mechanism subgroup is worse), it detects the effect only for "
                "f >= 0.30, and if the card works only through the mechanism it names then f on "
                "this stratum cannot exceed 0.17 -- 27 of the 159 tasks with room are "
                "settings-like. The significance test is therefore more likely than not to be "
                "inconclusive, structurally."),
        "justification": ("declaring this before the runs is the difference between a bounded "
                          "result and a failed one. Three deliverables are certain regardless of "
                          "the test: the first complete population pass rate for Astra on the "
                          "corrected guest; a no-harm claim on 219 tasks never used to design the "
                          "card, well powered because it is a monotone count; and an interval that "
                          "turns the in-sample '+20 points on class F' into 'at most +X on tasks "
                          "never used to design it'. An inconclusive test then has a reading of "
                          "its own: the card's effect does not extend materially beyond the task "
                          "family it was written for."),
        "cost": ("the campaign can no longer claim a confirmed population-level effect unless the "
                 "card works well beyond its named mechanism, which is exactly what the screen "
                 "measures."),
    },
]

# The four stages the user confirmed on 2026-11-12. Astra only: Sonnet runs on a separate quota and
# is not part of X22's claims (it may mirror stage 3 later, in its own trees, declared separately).
STAGES = (
    {"stage": 1, "set": "heldoutP", "arms": ["a0", "e1card2", "e1b2", "code2"],
     "role": "pre-registered confirmation of X16 and X20, fresh and interleaved; isolates card, "
             "evaluator paragraph and tool against the same baseline"},
    {"stage": 2, "set": "heldoutCsat", "arms": ["a0", "e1card2"],
     "role": "no-harm screen on never-cited tasks the reference model always passed"},
    {"stage": 3, "set": "population361", "arms": ["a0", "e1card2"],
     "role": "PRIMARY SCIENTIFIC RESULT: population screen, paired by task"},
    {"stage": 4, "set": "heldoutC", "arms": ["a0", "e1card2"],
     "role": "recorded and declared underpowered (6 tasks: the held-out pool is exhausted, E6)"},
)

# Stage 3's analysis, fixed now. Two asymmetric binary definitions, because the two questions are:
# a gain is a task the arm rescues (lenient), harm is a task the arm breaks (strict). With 2 runs
# per task neither definition is a majority, so both are stated rather than one being implied.
STAGE3_DESIGN = {
    "unit": "task",
    "runs_per_task": 2,
    "solved_for_gain": ">=1 of 2 runs pass",
    "solved_for_harm": "2 of 2 runs pass",
    # amendment 2: the primary deliverables are the bounded estimate and the no-harm
    # claim; the significance test is a declared secondary, underpowered below f = 0.20
    # (gates/power_check.py, run before any X22 run existed).
    "primary_deliverables": [
        "bounded effect estimate: mean paired per-task difference with a task-clustered "
        "bootstrap 95% interval, reported as an upper bound on the card's population effect",
        "no-harm at population scale: the count of tasks the baseline solves and the arm breaks "
        "under the strict definition; a monotone count, well powered at this n",
        "the first complete population pass rate for Astra on the corrected guest (A0 alone)",
    ],
    "secondary_test": ("one-sided exact McNemar on discordant tasks (b ~ Binomial(b+c, 0.5) "
                       "under H0), DECLARED UNDERPOWERED below f = 0.20: power 0.19 at f=0.10, "
                       "0.55 at 0.20, 0.88 at 0.30"),
    "inconclusive_reading": ("an inconclusive test is reported as a finding, not a gap: the "
                             "card's effect does not extend materially beyond the task family "
                             "it was written for"),
    # amendment 1: see AMENDMENTS. `out_of_design` is the population minus the tasks of a Phase 3
    # set; the stricter strata below stay as pre-registered sensitivities.
    "primary_stratum": "out_of_design",
    "sensitivity_strata": ["primary_out_of_sample", "never_cited", "all361"],
    "looks_at_task_fractions": [1 / 3, 2 / 3, 1.0],
    "look_rule": ("a task enters a look only when all its runs are complete on BOTH arms; between "
                  "two looks the data are not examined, and the look register is append-only"),
    "futility": ("at each look, assume every task still to complete turns discordant in the arm's "
                 "favour; if the exact McNemar p with (b + n_remaining, c) still does not cross the "
                 "final boundary, the arm cannot win and the stage stops. Arithmetic, not "
                 "probabilistic"),
    "harm_rule": ("stop and report harm if, under solved_for_harm, c - b crosses the mirror "
                  "boundary, or the mean paired difference is <= -10 points with the interval "
                  "entirely below 0"),
    "confirmation_of_harm": ("any task that flips negative under solved_for_harm is re-run at 5 "
                             "runs before being called harm: at 2 runs a flip can be noise"),
    "boundaries": None,   # filled by gates/calibrate.py, frozen before the first look
}


def cited(docs):
    out = set()
    for rel in docs:
        path = ROOT / rel
        if not path.exists():
            raise SystemExit(f"{rel} is missing: the strata would be computed on a short list")
        out |= set(_ID8.findall(path.read_text(errors="replace")))
    return out


def population():
    ids = [l.strip() for l in POPULATION.read_text().splitlines()
           if l.strip() and not l.lstrip().startswith("#")]
    lock = json.loads((HERE / "astra_official361_lock.json").read_text())["population"]
    digest = hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()
    if digest != lock["sha256"] or len(ids) != lock["count"]:
        raise SystemExit("population file does not match the frozen campaign lock")
    return ids, digest


def strata():
    ids, digest = population()
    sets = json.loads((config.DATA_DIR / "phase3_sets.json").read_text())["sets"]
    in_set = {t for name, s in sets.items() if name != "population361" for t in s["tasks"]}
    arm_cited, log_cited = cited(ARM_DOCS), cited(LOG_DOCS)
    out_of_design = [t for t in ids if t not in in_set]
    primary = [t for t in out_of_design if t[:8] not in arm_cited]
    never = [t for t in primary if t[:8] not in log_cited]
    return {"population_sha256": digest, "all361": ids, "out_of_design": out_of_design,
            "primary_out_of_sample": primary, "never_cited": never}


def build():
    # phase3 reads the campaign from the environment at import time, and group() refuses a set
    # that belongs to the other campaign -- so the lock must be generated inside its own campaign.
    if phase3.SUFFIX != SUFFIX:
        raise SystemExit(f"run with OSW_PHASE3_SUFFIX={SUFFIX} (phase3 is on {phase3.SUFFIX!r})")
    s = strata()
    sets = phase3.load_sets()
    groups = []
    for stage in STAGES:
        for arm in stage["arms"]:
            g = phase3.group("astra", arm, stage["set"], sets)
            groups.append({"stage": stage["stage"], "model": "astra", "arm": arm,
                           "set": stage["set"], "tree": g["tree"],
                           "tasks": len(g["tasks"]), "runs_per_task": g["runs"],
                           "runs": len(g["tasks"]) * g["runs"]})
    return {
        "campaign": "x22",
        "about": ("Confirmatory campaign of Phase 3, allocation confirmed by the user on "
                  "2026-11-12. Gate A refuses any group this lock does not declare."),
        "fixed_on": "2026-11-12",
        "harness_tag": TAG,
        "tree_suffix": SUFFIX,
        "model": config.ASTRA_MODEL,
        "reasoning_effort": config.ASTRA_REASONING_EFFORT,
        "codex_cli_version": config.ASTRA_CODEX_VERSION,
        "image": config.SUFFIX_IMAGES[SUFFIX],
        "evaluator_commit": EVALUATOR_COMMIT,
        "max_steps": config.MAX_STEPS,
        "screen_size": f"{config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}",
        "arm_addendum_sha256": {a: arms.provenance(a)["arm_addendum_sha256"]
                                for a in ("e1card2", "e1b2", "code2")},
        "arm_tools": {a: list(arms.tools(a)) for a in ("e1card2", "e1b2", "code2")},
        "amendments": AMENDMENTS,
        "stages": list(STAGES),
        "groups": groups,
        "total_runs": sum(g["runs"] for g in groups),
        "stage3": {**STAGE3_DESIGN,
                   "strata_sizes": {k: len(v) for k, v in s.items() if isinstance(v, list)},
                   "population_sha256": s["population_sha256"],
                   "strata": {k: v for k, v in s.items() if isinstance(v, list)}},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--amend", action="store_true",
                    help="rewrite an existing lock; refused once a boundary or a run exists")
    a = ap.parse_args(argv)
    lock = build()
    print(f"campaign {lock['campaign']}, tag {lock['harness_tag']}, suffix {lock['tree_suffix']}")
    for g in lock["groups"]:
        print(f"  stage {g['stage']}  {g['arm']:8} {g['set']:14} "
              f"{g['tasks']:3} x {g['runs_per_task']} = {g['runs']:4} runs")
    print(f"  TOTAL {lock['total_runs']} runs")
    print(f"  stage-3 strata: {lock['stage3']['strata_sizes']}")
    if not a.write:
        print("\n(dry run: pass --write)")
        return
    if LOCK.exists():
        # A lock may be amended only while nothing depends on it yet: no frozen look boundary
        # (so no look can have been taken) and no results tree of this campaign on disk.
        current = json.loads(LOCK.read_text())
        existing = sorted((config.RESULTS_DIR).glob(f"*_{SUFFIX}*_official"))
        if existing:
            raise SystemExit(f"{LOCK.name}: results already exist for this campaign "
                             f"({len(existing)} trees); the lock is never regenerated")
        if not a.amend:
            raise SystemExit(f"{LOCK.name} exists; pass --amend to rewrite it (allowed only "
                             "while no run of the campaign exists)")
        # Frozen boundaries are evidence, not a draft: carry them over instead of recomputing, so
        # an amendment can never silently re-roll the look schedule or the alphas.
        frozen = (current.get("stage3") or {}).get("boundaries")
        if frozen is not None:
            lock["stage3"]["boundaries"] = frozen
            print("carried over the frozen look boundaries unchanged")
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"\nwritten {LOCK}")


if __name__ == "__main__":
    main()
