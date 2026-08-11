"""G6 -- maps OSWorld tasks onto a QA-testing competency taxonomy -> RQ7 (construct validity).
Analytical, offline, no execution data needed -- works straight off the task JSON.

Six competencies, checked in this priority order (a task gets one label):
  infeasibility_recognition -- evaluator.func == "infeasible" (reuses g0_evaluator_audit's
                                classification, not re-derived)
  oracle_verification       -- instruction explicitly asks for a check/confirmation of the
                                resulting state (STRICT reading, decided 2026-08-09: the
                                EVALUATOR comparing actual-vs-expected doesn't count on its
                                own -- that's the harness's oracle, not a competency the task
                                asks the AGENT to exercise. Only counted when the instruction
                                itself asks for verification.)
  bug_reproduction          -- instruction describes reproducing/diagnosing a defect
  regression_consistency    -- instruction asks that a change not break something else, or
                                that repeated behavior stay consistent
  exploratory_testing       -- declared structurally absent, not pattern-matched: every
                                OSWorld task ships a fixed instruction, which is the opposite
                                of unscripted probing. 0 by construction, not by search.
  test_execution            -- default bucket: everything else. No further reproducible rule
                                separates "executing a QA-relevant procedure" from "pure GUI
                                manipulation" within this bucket -- forcing one would need
                                semantic judgment per task, not pattern matching, so this
                                module doesn't claim a split it can't validate.

Binary scoring (no partial credit) is a benchmark-wide property, not a per-task label -- it's
declared once in the deliverable text, not tagged here.

Keyword patterns were checked by hand against every match on the full 369-task set (small
enough to review exhaustively, not just sample), not assumed. One false positive found and
excluded explicitly (see BUG_REPRODUCTION_FALSE_POSITIVES): "my glasses are broken" matches
the word "broken" but has nothing to do with a software defect.

Run: python -m benchmarks.osworld.analysis.g6_qa_construct_mapping
"""
import re
from collections import Counter

from benchmarks.osworld.analysis import g0_evaluator_audit as g0

ORACLE_VERIFICATION_RE = re.compile(r"\b(verify|make sure|ensure|confirm|check whether|check if)\b", re.I)
BUG_REPRODUCTION_RE = re.compile(r"\b(reproduce|bug|error occurs?|not working|broken|crash(?:es|ed)?)\b", re.I)
REGRESSION_CONSISTENCY_RE = re.compile(
    r"\b(without breaking|still works?|after updating|regression|no longer works?)\b", re.I)

# Found by manually reviewing every BUG_REPRODUCTION_RE match on the full dataset (2026-08-09):
# "broken" here means the user's glasses, not a software defect.
BUG_REPRODUCTION_FALSE_POSITIVES = {"3ce045a0-877b-42aa-8d2c-b4a863336ab8"}

COMPETENCIES = ("infeasibility_recognition", "oracle_verification", "bug_reproduction",
                "regression_consistency", "test_execution")


def classify(task, classes):
    if g0.task_status(task, classes) == "infeasible":
        return "infeasibility_recognition"
    instr = task["instruction"]
    if task["id"] in BUG_REPRODUCTION_FALSE_POSITIVES:
        pass
    elif BUG_REPRODUCTION_RE.search(instr):
        return "bug_reproduction"
    if REGRESSION_CONSISTENCY_RE.search(instr):
        return "regression_consistency"
    if ORACLE_VERIFICATION_RE.search(instr):
        return "oracle_verification"
    return "test_execution"


def matrix():
    """{app: Counter({competency: n})} across the full dataset, plus a totals row."""
    tasks = g0._load_tasks()
    classes = g0.classify_all()
    out = {}
    for t in tasks:
        apps = t.get("related_apps") or ["misc"]
        app = apps[0]
        c = classify(t, classes)
        out.setdefault(app, Counter())[c] += 1
    return out


def _main():
    m = matrix()
    total = Counter()
    for counts in m.values():
        total.update(counts)
    n = sum(total.values())

    print(f"G6 -- QA construct mapping ({n} tasks)\n")
    print(f"{'competency':<28} {'n':>5}  {'%':>6}")
    for c in COMPETENCIES:
        print(f"{c:<28} {total[c]:>5}  {100 * total[c] / n:5.1f}%")
    print(f"{'exploratory_testing':<28} {0:>5}  {0.0:5.1f}%  (structurally absent, not searched)")

    print("\nPer app:")
    for app in sorted(m):
        row = m[app]
        tot = sum(row.values())
        parts = ", ".join(f"{c}={row[c]}" for c in COMPETENCIES if row[c])
        print(f"  {app:<20} n={tot:<4} {parts}")


if __name__ == "__main__":
    _main()
