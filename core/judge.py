"""The `Judge` seam + `claude -p` judge plumbing.

A Judge maps `(task, answer, ref, out) -> {"verdict": "SUCCESS"|"FAILURE", "reason": ...}`.
There are two kinds, and the whole point of the seam is that they don't share an
implementation:

  - **LLM judges** (WebVoyager's screenshot judge, BrowseComp's answer-grader) are built on
    the `claude -p` plumbing here (`llm_verdict`) and are NONDETERMINISTIC — `core.run`
    repeats them to measure judge variance.
  - **Deterministic functional checkers** (WebArena's string/url/DOM check) import NONE of
    this; they wrap a pure function in `Judge(is_deterministic=True)` so `core.run` calls
    them exactly once.

`is_deterministic` is the contract `core.run` reads to decide repeat-vs-once.
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from core.agent_loop import build_claude_cmd, run_claude

VERDICT_RE = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}")


@dataclass
class Judge:
    """A scoring strategy. `fn(task, answer, ref, out) -> {'verdict', 'reason'}`."""
    fn: Callable[..., dict]
    is_deterministic: bool = False

    def __call__(self, task, answer, ref, out) -> dict:
        return self.fn(task, answer, ref, out)


def parse_verdict(text: str):
    """Extract (verdict, reason) from judge output; tolerant of stray prose around JSON."""
    matches = VERDICT_RE.findall(text or "")
    if matches:
        try:
            obj = json.loads(matches[-1])
            v = str(obj.get("verdict", "")).upper()
            return ("SUCCESS" if v.startswith("SUCC") else "FAILURE", obj.get("reason", ""))
        except Exception:
            pass
    up = (text or "").upper()
    if "SUCCESS" in up and "FAILURE" not in up:
        return ("SUCCESS", "parsed from text")
    return ("FAILURE", "could not parse verdict")


def llm_verdict(prompt, *, out, model=None, timeout=180, max_turns=12, retries=3) -> dict:
    """Run a `claude -p` judge pass and parse its verdict, retrying on unparseable output.

    The building block for every LLM judge: the benchmark supplies the prompt (and may let
    the judge Read screenshots under `out`), this returns the parsed verdict dict. Writing
    `eval.json` is `core.results`' job, not the judge's — so the same trajectory can be
    judged repeatedly for variance without clobbering anything.

    `retries`: an empty/garbled judge response (e.g. when the subscription is rate-limited
    under concurrency) parses to "could not parse verdict" and would otherwise be silently
    counted as a FAILURE. That is a JUDGE failure, not an agent failure, so we re-ask a few
    times with linear backoff before giving up — without this, a transient rate-limit turns
    into a bogus FAILURE verdict.
    """
    cmd = build_claude_cmd(prompt, model=model, max_turns=max_turns, add_dir=out)
    result = {"verdict": "FAILURE", "reason": "could not parse verdict"}
    for attempt in range(max(1, retries)):
        if attempt:
            time.sleep(5 * attempt)   # let a transient rate-limit clear before re-asking
        verdict, reason = parse_verdict(run_claude(cmd, timeout=timeout))
        result = {"verdict": verdict, "reason": reason}
        if reason != "could not parse verdict":
            break
    return result
