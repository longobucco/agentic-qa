"""WebVoyager's canonical screenshot judge — one implementation of the `core.judge.Judge` seam.

The benchmark OWNS the judge: the SAME screenshot-based protocol is applied to every runner
(agent_browser, alumnium, ...), so a runner cannot change how it's scored. Each runner is
therefore responsible for producing screenshots into `out` (agent_browser saves them via
agent-browser; alumnium collects Alumnium's full-page screenshots out of its container).
There is intentionally no text fallback and no reference answer — that's what makes the
score comparable across systems instead of dependent on what a runner happened to emit.

The `claude -p` invocation and verdict parsing are `core.judge` plumbing. It's marked
nondeterministic, so `core.run` re-judges for variance when asked (`--judge-repeats`).
"""
from benchmarks.webvoyager import config
from benchmarks.webvoyager.prompts import judge_prompt
from core.judge import Judge, llm_verdict


def screenshot_judge(task, answer, ref, out):
    """Canonical WebVoyager scoring: task + answer + the last screenshots under `out`.

    `ref` is part of the `Judge` seam signature but intentionally UNUSED — WebVoyager judges
    from screenshots, not a gold string. If a runner produced no screenshots, the judge sees
    that and returns FAILURE (it cannot verify the claim)."""
    prompt = judge_prompt(task, answer, out)
    return llm_verdict(prompt, out=out, model=config.MODEL or None,
                       timeout=config.JUDGE_TIMEOUT, max_turns=12)


JUDGE = Judge(fn=screenshot_judge, is_deterministic=False)
