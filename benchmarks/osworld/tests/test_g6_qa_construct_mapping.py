"""Unit tests for g6_qa_construct_mapping.py (offline, no execution data needed):
  python -m benchmarks.osworld.tests.test_g6_qa_construct_mapping
"""
from benchmarks.osworld.analysis import g6_qa_construct_mapping as g6
from benchmarks.osworld.analysis import g0_evaluator_audit as g0


def test_infeasible_wins_over_keyword_matches():
    """evaluator.func == infeasible must take priority even if the instruction ALSO contains
    verification language -- infeasibility recognition is the more specific, more important
    signal for RQ7, not a competing keyword match."""
    task = {"id": "x", "instruction": "Please verify this works.",
            "evaluator": {"func": "infeasible"}}
    classes = {"infeasible": ("infeasible", "manual")}
    assert g6.classify(task, classes) == "infeasibility_recognition"


def test_oracle_verification_keyword():
    task = {"id": "x", "instruction": "Ensure the alignment is applied correctly.",
            "evaluator": {"func": "exact_match"}}
    assert g6.classify(task, {}) == "oracle_verification"


def test_bug_reproduction_keyword():
    task = {"id": "x", "instruction": "The program will crash, please fix the bugs of code.",
            "evaluator": {"func": "exact_match"}}
    assert g6.classify(task, {}) == "bug_reproduction"


def test_bug_reproduction_false_positive_is_excluded():
    """'My glasses are broken' matches the word 'broken' but isn't a software defect -- the
    real false positive found by hand-reviewing every match on the full dataset."""
    fp_id = next(iter(g6.BUG_REPRODUCTION_FALSE_POSITIVES))
    task = {"id": fp_id, "instruction": "My glasses are broken, enlarge the text.",
            "evaluator": {"func": "exact_match"}}
    assert g6.classify(task, {}) == "test_execution"


def test_regression_consistency_keyword():
    task = {"id": "x", "instruction": "Make sure this still works after updating the driver.",
            "evaluator": {"func": "exact_match"}}
    assert g6.classify(task, {}) == "regression_consistency"


def test_default_bucket_is_test_execution():
    task = {"id": "x", "instruction": "Change the background color to blue.",
            "evaluator": {"func": "exact_match"}}
    assert g6.classify(task, {}) == "test_execution"


def test_matrix_covers_every_real_task_exactly_once():
    tasks = g0._load_tasks()
    m = g6.matrix()
    assert sum(sum(c.values()) for c in m.values()) == len(tasks)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
