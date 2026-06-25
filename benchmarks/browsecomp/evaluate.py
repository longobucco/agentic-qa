"""BrowseComp's answer-vs-reference grader — the SECOND implementation of the `core.judge`
seam (WebVoyager's screenshot judge is the first). It reuses the `claude -p` invoke +
JSON-verdict parsing from `core.judge`, but the evidence is purely TEXT: the question, the
agent's answer, and the withheld reference answer. No screenshots, no `out` files. It's
nondeterministic (an LLM), so `core.run` may re-judge for variance via `--judge-repeats`.
"""
from benchmarks.browsecomp import config
from benchmarks.browsecomp.prompts import grader_prompt
from core.judge import Judge, llm_verdict


def answer_grader(task, answer, ref, out):
    """Grade the agent's answer against the reference (text only). `out` is part of the Judge
    seam signature but unused here — there are no screenshots to read."""
    prompt = grader_prompt(task, answer, ref)
    return llm_verdict(prompt, out=out, model=config.MODEL or None,
                       timeout=config.JUDGE_TIMEOUT, max_turns=3)


JUDGE = Judge(fn=answer_grader, is_deterministic=False)
