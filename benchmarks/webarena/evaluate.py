"""WebArena's DETERMINISTIC functional checker — the THIRD implementation of the `core.judge`
seam, and the one that proves the seam is real: it imports **none** of `core.judge`'s
`claude -p` plumbing. No LLM, no screenshots — just string / url / DOM checks against the
task's eval spec. `is_deterministic=True`, so `core.run` calls it **exactly once** (no
re-judging for variance).

WebArena eval spec (per task, under `task["eval"]`):
    eval_types: ["string_match" | "url_match" | "program_html", ...]
    reference_answers: {"exact_match": str} | {"must_include": [str]} | {"fuzzy_match": [str]}
    reference_url: "<url>"   (supports "|OR|" alternatives; GOLD-in-PRED matching)
    program_html: [{"url", "locator", "required_contents": {"exact_match"|"must_include"}}]

Live-stack inputs (final URL, and each program_html target's located text) are captured by the
runner into `result.json` while the stack is up; this judge then scores them with PURE
functions — so the scoring logic is unit-testable here with no stack (see tests/). The
matching mirrors WebArena's `evaluation_harness/evaluators.py`.
"""
import json

from core.judge import Judge

OR = "|OR|"


# ---- pure deterministic primitives (unit-tested locally; no stack, no LLM) ----

def clean_answer(s):
    """WebArena normalization: strip, drop wrapping quotes, lowercase."""
    s = (s or "").strip()
    if len(s) > 1 and s[0] in "'\"" and s[-1] in "'\"":
        s = s[1:-1]
    return s.lower()


def exact_match(pred, ref):
    return clean_answer(pred) == clean_answer(ref)


def must_include(pred, ref, *, tokenize=False):
    """Cleaned `ref` appears in cleaned `pred`. For single-word refs, match a whole token
    (not a substring) to avoid false positives, mirroring WebArena's tokenized check. Tokens
    have wrapping punctuation stripped ('confirmed!' -> 'confirmed') but keep internal chars
    ('$42.00' stays intact)."""
    p, r = clean_answer(pred), clean_answer(ref)
    if tokenize:
        toks = [t.strip(".,!?;:'\"") for t in p.split()]
        return r in toks
    return r in p


def _content_ok(text, required):
    """A program_html `required_contents` check against already-fetched element `text`."""
    if "exact_match" in required:
        return exact_match(text, required["exact_match"])
    if "must_include" in required:
        # each item must appear; an item may offer |OR| alternatives
        return all(any(must_include(text, alt) for alt in item.split(OR))
                   for item in required["must_include"])
    return False


def string_score(pred, reference_answers):
    """Score the predicted answer against reference_answers. Deterministic approaches only
    (exact_match / must_include); fuzzy_match needs an LLM and is out of scope for this
    deterministic checker (raise so it's never silently passed)."""
    score = 1.0
    for approach, ref in reference_answers.items():
        if approach == "exact_match":
            score *= float(exact_match(pred, ref))
        elif approach == "must_include":
            for item in ref:
                tok = len(item.split()) == 1
                score *= float(any(must_include(pred, alt, tokenize=tok)
                                   for alt in item.split(OR)))
        elif approach == "fuzzy_match":
            raise NotImplementedError(
                "fuzzy_match requires an LLM grader; the deterministic checker covers "
                "exact_match/must_include/url_match/program_html only."
            )
    return score


def url_score(pred_url, reference_url):
    """GOLD-in-PRED: each ref's base path must be a substring of pred's, and every ref query
    value must appear in pred's query. Supports `|OR|` alternative reference URLs."""
    from urllib.parse import urlparse, parse_qs

    def parts(u):
        u = (u or "").rstrip("/")
        p = urlparse(u)
        return p.netloc + p.path, parse_qs(p.query)

    pred_base, pred_q = parts(pred_url)
    best = 0.0
    for ref in (reference_url or "").split(OR):
        ref_base, ref_q = parts(ref)
        base = 1.0 if ref_base in pred_base else 0.0
        q = 1.0
        for k, vals in ref_q.items():
            q *= float(any(v in pred_q.get(k, []) for v in vals))
        best = max(best, base * q)
    return best


# ---- the Judge: orchestrate the spec over runner-captured, live-stack inputs ----

def _captured(out):
    p = out / "result.json"
    return json.loads(p.read_text()) if p.exists() else {}


def webarena_check(task, answer, ref, out):
    """Deterministic verdict. `ref` is unused (the spec lives on the task). Live-stack values
    (final_url, program_html located contents) are read from the runner's result.json."""
    spec = task["eval"]
    types = spec.get("eval_types", [])
    captured = _captured(out)
    score = 1.0
    notes = []

    if "string_match" in types:
        s = string_score(answer or "", spec.get("reference_answers", {}))
        score *= s
        notes.append(f"string={s:.0f}")

    if "url_match" in types:
        final_url = captured.get("final_url", "")
        s = url_score(final_url, spec.get("reference_url", ""))
        score *= s
        notes.append(f"url={s:.0f}")

    if "program_html" in types:
        fetched = captured.get("program_html_contents")
        if fetched is None:
            score = 0.0
            notes.append("program_html=NO-CAPTURE")
        else:
            s = 1.0
            for target, text in zip(spec["program_html"], fetched):
                s *= float(_content_ok(text, target["required_contents"]))
            score *= s
            notes.append(f"html={s:.0f}")

    verdict = "SUCCESS" if score >= 1.0 and types else "FAILURE"
    return {"verdict": verdict, "reason": f"deterministic check ({', '.join(notes) or 'no eval'})"}


JUDGE = Judge(fn=webarena_check, is_deterministic=True)
