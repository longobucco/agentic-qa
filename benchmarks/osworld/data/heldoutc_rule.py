"""The selection rule of the X22 confirmatory held-out sets. This script IS the rule.

Written and committed BEFORE being applied, so the sets cannot have been chosen on their outcomes:
re-running it reproduces them exactly. `--write` inserts the result into data/phase3_sets.json;
without it the script only prints what it would select.

Two sets, from one eligible pool:
  heldoutC     -- the effect set (primary of X22), tasks that still have room
  heldoutCsat  -- the no-harm screen, tasks the reference model already passed every time

Eligible pool, over the frozen 361-task population (scripts/g_protocol361_ids.txt):
  (a) EVERY entry of the evaluator's `result` is of type vm_file or vm_command_line -- the checker
      reads persisted state, which is also what V1's final-state capture records, so a disagreement
      between checker and verifier can be adjudicated from the saved artefacts;
  (b) not infeasible by design (no `infeasible` in the evaluator's `func`);
  (c) in no set of data/phase3_sets.json;
  (d) never cited in any analysis document (CITING_DOCS below) -- so never used to write an arm,
      and never named in the proposal that led to X22;
  (e) not one of the seven G17 tasks a null agent passes;
  (f) the reference model (Claude Sonnet 5, original image, column `sonnet5-new_pass/valid` of
      campaign_analysis/tables/task_matrix.csv) has at least MIN_VALID valid runs.

Then, among the pool:
  heldoutC    = passes <  valid   (room to improve)
  heldoutCsat = passes == valid   (saturated)
both taken round-robin by application bucket over ids sorted ascending, capped at MAX_C and
MAX_SAT. The caps are declared here, before the rule runs, so a large pool is truncated
mechanically rather than by choice.

Declared limitation, carried into the pre-registration: (f) conditions on an outcome of the OTHER
model, which is quasi-independent for Astra but not fully -- both models suffer the same guest and
checker defects, so the filter selects partly for tasks with environment problems. X22 therefore
pre-registers a secondary analysis restricted to tasks not labelled uncontrollable in
tables/e3_controllability.csv, and reports the controllability composition of the set.
"""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TASKS = HERE / "osworld_verified.jsonl"
SETS = HERE / "phase3_sets.json"
POPULATION = ROOT / "scripts" / "g_protocol361_ids.txt"
MATRIX = HERE.parent / "campaign_analysis" / "tables" / "task_matrix.csv"
CONTROLLABILITY = HERE.parent / "campaign_analysis" / "tables" / "e3_controllability.csv"

PERSISTED_RESULT_TYPES = {"vm_file", "vm_command_line"}
MIN_VALID = 3
MAX_C = 60
MAX_SAT = 40
RUNS_C = 5
RUNS_SAT = 2
REFERENCE_COLUMN = "sonnet5-new_pass/valid"

# G17 (EXPERIMENTS.md X6): the tasks the null agent passes. A SUCCESS there shows no capability and
# a FAIL destroys an already-passing initial state, so they can never be part of an effect set.
G17 = ("b6781586", "28cc3b7e", "99146c54", "3299584d", "4d117223", "93eabf48", "9219480b")

# Every document that could have informed an arm, plus the proposal that led to X22. All are local
# thesis material except the last; a missing file is fatal, because a silently shorter exclusion
# list would widen the pool.
CITING_DOCS = (
    "benchmarks/osworld/campaign_analysis/BUG_REGISTER.md",
    "benchmarks/osworld/campaign_analysis/ASTRA_ANALYSIS.md",
    "benchmarks/osworld/campaign_analysis/ASTRA_IMPROVEMENT_RESEARCH.md",
    "benchmarks/osworld/campaign_analysis/EXPERIMENTS.md",
    "benchmarks/osworld/campaign_analysis/FINDINGS.md",
    "benchmarks/osworld/campaign_analysis/METHODOLOGY.md",
    "benchmarks/osworld/campaign_analysis/RESEARCH_LOG.md",
    "benchmarks/osworld/campaign_analysis/SUMMARY.md",
    "benchmarks/osworld/campaign_analysis/F1_ASTRA_SHEETS.md",
    "benchmarks/osworld/campaign_analysis/F1_SONNET_STEPCAP_AUDIT.md",
    "benchmarks/osworld/campaign_analysis/astra_ledger.json",
    "docs/2026-11-12_harness_astra_proposta.md",
)
_ID8 = re.compile(r"\b([0-9a-f]{8})\b")


def population():
    ids = [l.strip() for l in POPULATION.read_text().splitlines()
           if l.strip() and not l.lstrip().startswith("#")]
    digest = hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()
    lock = json.loads((HERE.parent / "astra_official361_lock.json").read_text())["population"]
    if digest != lock["sha256"] or len(ids) != lock["count"]:
        raise SystemExit("population file does not match the frozen campaign lock")
    return ids


def cited_prefixes():
    """The 8-hex id prefixes named anywhere in the analysis documents."""
    cited = set()
    for rel in CITING_DOCS:
        path = ROOT / rel
        if not path.exists():
            raise SystemExit(f"{rel} is missing: the exclusion list would be incomplete")
        cited |= set(_ID8.findall(path.read_text(errors="replace")))
    return cited


def persisted_only(task):
    result = (task.get("evaluator") or {}).get("result")
    entries = result if isinstance(result, list) else [result]
    types = [e.get("type") for e in entries if isinstance(e, dict)]
    return bool(types) and all(t in PERSISTED_RESULT_TYPES for t in types)


def feasible(task):
    return "infeasible" not in json.dumps((task.get("evaluator") or {}).get("func"))


def reference_outcome(row):
    """(passes, valid) from the reference column, or None when it is empty."""
    cell = (row.get(REFERENCE_COLUMN) or "").strip()
    if "/" not in cell:
        return None
    a, b = cell.split("/", 1)
    try:
        return int(a), int(b)
    except ValueError:
        return None


def round_robin(items, bucket_of, limit):
    """Ids sorted ascending, taken one per bucket in turn, truncated at `limit`."""
    by_bucket = {}
    for i in sorted(items):
        by_bucket.setdefault(bucket_of[i], []).append(i)
    out, order = [], sorted(by_bucket)
    while len(out) < limit and any(by_bucket[b] for b in order):
        for b in order:
            if by_bucket[b] and len(out) < limit:
                out.append(by_bucket[b].pop(0))
    return out


def select():
    tasks = {t["id"]: t for t in (json.loads(l) for l in TASKS.read_text().splitlines() if l.strip())}
    pop = population()
    existing = {t for s in json.loads(SETS.read_text())["sets"].values() for t in s["tasks"]}
    cited = cited_prefixes()
    matrix = {r["task_id"]: r for r in csv.DictReader(MATRIX.open())}

    pool, bucket_of, reasons = [], {}, {"persisted": 0, "feasible": 0, "in_a_set": 0,
                                       "cited": 0, "g17": 0, "no_reference": 0}
    for tid in pop:
        task = tasks.get(tid)
        if task is None or not persisted_only(task):
            reasons["persisted"] += 1; continue
        if not feasible(task):
            reasons["feasible"] += 1; continue
        if tid in existing:
            reasons["in_a_set"] += 1; continue
        if tid[:8] in cited:
            reasons["cited"] += 1; continue
        if tid[:8] in G17:
            reasons["g17"] += 1; continue
        row = matrix.get(tid)
        outcome = reference_outcome(row) if row else None
        if outcome is None or outcome[1] < MIN_VALID:
            reasons["no_reference"] += 1; continue
        pool.append((tid, outcome))
        bucket_of[tid] = (row.get("bucket") or "?").strip()

    room = [t for t, (p, v) in pool if p < v]
    sat = [t for t, (p, v) in pool if p == v]
    return {
        "pool": len(pool), "excluded": reasons, "cited_prefixes": len(cited),
        "bucket_of": bucket_of,
        "heldoutC": round_robin(room, bucket_of, MAX_C),
        "heldoutCsat": round_robin(sat, bucket_of, MAX_SAT),
        "room_total": len(room), "sat_total": len(sat),
    }


RULE_TEXT = (
    "Fixed and committed on 2026-11-12 as benchmarks/osworld/data/heldoutc_rule.py, applied once "
    "before any X22 run. Eligible pool over the frozen 361-task population: every entry of the "
    "evaluator's `result` is vm_file or vm_command_line; not infeasible by design; in no other set "
    "of this file; never cited in any analysis document or in the 12/11 harness proposal "
    "(8-hex id match); not one of the seven G17 null-agent-pass tasks; the reference model "
    "(Claude Sonnet 5, original image, column sonnet5-new_pass/valid of tables/task_matrix.csv) has "
    "at least 3 valid runs. heldoutC = pool tasks with passes < valid, round-robin by application "
    "bucket over ids sorted ascending, capped at 60. heldoutCsat = pool tasks with passes == valid, "
    "same order, capped at 40. Caps declared in the script before it was run. Limitation carried "
    "into the pre-registration: the room filter conditions on the other model's outcome, which is "
    "quasi-independent for Astra but selects partly for shared guest and checker defects; X22 "
    "reports the controllability composition and a secondary analysis excluding uncontrollable "
    "tasks."
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="insert the sets into phase3_sets.json")
    a = ap.parse_args(argv)
    s = select()
    print(f"eligible pool: {s['pool']} tasks  (with room {s['room_total']}, saturated {s['sat_total']})")
    print(f"excluded: {s['excluded']}")
    print(f"id prefixes cited in the analysis documents: {s['cited_prefixes']}")
    for name, ids, runs in (("heldoutC", s["heldoutC"], RUNS_C),
                            ("heldoutCsat", s["heldoutCsat"], RUNS_SAT)):
        buckets = {}
        for i in ids:
            buckets[s["bucket_of"][i]] = buckets.get(s["bucket_of"][i], 0) + 1
        print(f"\n{name}: {len(ids)} tasks x {runs} runs = {len(ids) * runs} runs per arm")
        print(f"  by application: {buckets}")
    if not a.write:
        print("\n(dry run: pass --write to insert into phase3_sets.json)")
        return
    doc = json.loads(SETS.read_text())
    if "heldoutC" in doc["sets"] or "heldoutCsat" in doc["sets"]:
        raise SystemExit("heldoutC/heldoutCsat already in phase3_sets.json: a frozen set is never "
                         "recomputed (delete them by hand if a re-freeze is really intended)")
    for name, ids, runs in (("heldoutC", s["heldoutC"], RUNS_C),
                            ("heldoutCsat", s["heldoutCsat"], RUNS_SAT)):
        doc["sets"][name] = {
            "fixed_on": "2026-11-12",
            "rule": RULE_TEXT,
            "generated_by": "benchmarks/osworld/data/heldoutc_rule.py",
            "eligible_pool": s["pool"], "with_room": s["room_total"], "saturated": s["sat_total"],
            "runs_per_task": runs,
            "tasks": ids,
        }
    SETS.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"\nwritten into {SETS}")


if __name__ == "__main__":
    main()
