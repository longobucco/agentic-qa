"""Unit tests for WebArena's deterministic checker — proves the third judge type works with
NO stack and NO LLM. Run: python -m benchmarks.webarena.tests.test_evaluate (or pytest)."""
import json
import tempfile
from pathlib import Path

from benchmarks.webarena import evaluate as ev


def _judge(task, answer, captured):
    """Run the deterministic judge with `captured` written as the runner's result.json."""
    out = Path(tempfile.mkdtemp(prefix="wa_test_"))
    (out / "result.json").write_text(json.dumps(captured))
    return ev.webarena_check(task, answer, None, out)["verdict"]


def test_primitives():
    assert ev.exact_match(" 'Yes' ", "yes")
    assert not ev.exact_match("yes", "no")
    assert ev.must_include("the total is $42.00 today", "$42.00")
    assert ev.must_include("order 7 shipped", "7", tokenize=True)
    assert not ev.must_include("order 73 shipped", "7", tokenize=True)  # token, not substring
    assert ev.url_score("http://shop/cat/5?page=2", "http://shop/cat/5") == 1.0
    assert ev.url_score("http://shop/cat/5?page=2", "/cat/5|OR|/cat/9") == 1.0
    assert ev.url_score("http://shop/cat/9", "/cat/5") == 0.0


def test_string_match_exact():
    task = {"eval": {"eval_types": ["string_match"],
                     "reference_answers": {"exact_match": "08/15/2023"}}}
    assert _judge(task, "08/15/2023", {}) == "SUCCESS"
    assert _judge(task, "2023-08-15", {}) == "FAILURE"


def test_string_match_must_include_with_or():
    task = {"eval": {"eval_types": ["string_match"],
                     "reference_answers": {"must_include": ["123 Main St", "Pittsburgh|OR|PGH"]}}}
    assert _judge(task, "Ship to 123 Main St, PGH 15213", {}) == "SUCCESS"
    assert _judge(task, "Ship to 123 Main St, Boston", {}) == "FAILURE"


def test_url_match():
    task = {"eval": {"eval_types": ["url_match"], "reference_url": "http://shop/cart"}}
    assert _judge(task, "", {"final_url": "http://shop/cart/"}) == "SUCCESS"
    assert _judge(task, "", {"final_url": "http://shop/home"}) == "FAILURE"


def test_program_html():
    task = {"eval": {"eval_types": ["program_html"],
                     "program_html": [
                         {"url": "last", "locator": "document.querySelector('.qty').innerText",
                          "required_contents": {"exact_match": "3"}}]}}
    assert _judge(task, "", {"program_html_contents": ["3"]}) == "SUCCESS"
    assert _judge(task, "", {"program_html_contents": ["5"]}) == "FAILURE"
    assert _judge(task, "", {}) == "FAILURE"  # nothing captured -> fail (can't verify)


def test_combined_and_determinism():
    task = {"eval": {"eval_types": ["string_match", "url_match"],
                     "reference_answers": {"must_include": ["confirmed"]},
                     "reference_url": "http://shop/checkout/success"}}
    captured = {"final_url": "http://shop/checkout/success?id=9"}
    assert _judge(task, "Order confirmed!", captured) == "SUCCESS"
    assert _judge(task, "Order pending", captured) == "FAILURE"      # string fails
    assert ev.JUDGE.is_deterministic is True                        # core.run -> judge once


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    passed = 0
    for t in TESTS:
        t()
        print(f"  ✓ {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(TESTS)} deterministic-judge tests passed (no stack, no LLM).")


if __name__ == "__main__":
    main()
