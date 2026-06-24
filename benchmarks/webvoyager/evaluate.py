"""WebVoyager's LLM screenshot judge — one implementation of the `core.judge.Judge` seam.

The benchmark-specific part is only the prompt + the screenshot evidence; the `claude -p`
invocation and verdict parsing are `core.judge` plumbing. It's marked nondeterministic, so
`core.run` will re-judge for variance when asked (`--judge-repeats`). BrowseComp's
answer-grader will be a second `Judge` on the same plumbing; WebArena's functional checker
a third that imports NONE of it.
"""
from benchmarks.webvoyager import config
from benchmarks.webvoyager.prompts import judge_prompt
from core.judge import Judge, llm_verdict


def screenshot_judge(task, answer, ref, out):
    """Score one trajectory from the answer + the last screenshots under `out`."""
    has_shots = any(out.glob("*.png"))
    prompt = judge_prompt(task, answer, ref, out, has_screenshots=has_shots)
    return llm_verdict(prompt, out=out, model=config.MODEL or None,
                       timeout=config.JUDGE_TIMEOUT, max_turns=12)


JUDGE = Judge(fn=screenshot_judge, is_deterministic=False)
