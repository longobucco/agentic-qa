"""Unit tests for core/agent_loop.py's subprocess handling:
  python -m core.tests.test_agent_loop
"""
import sys
import time

from core.agent_loop import _run_raw, extract_answer

_GRANDCHILD_HANG_S = 15   # worst-case regression runtime if the process-group kill breaks


def test_extract_answer_takes_the_last_match():
    text = "blah\nANSWER: first\nmore\nANSWER: second "
    assert extract_answer(text) == "second"


def test_extract_answer_empty_without_a_match():
    assert extract_answer("no answer line here") == ""


def test_run_raw_returns_stdout_on_normal_exit():
    out = _run_raw([sys.executable, "-c", "print('hello')"], timeout=10)
    assert out.strip() == "hello"


def test_run_raw_kills_the_whole_process_group_on_timeout():
    """Regression for a real bug: `claude -p` spawns an MCP server as a grandchild that
    inherits the stdout pipe. A plain `subprocess.run(..., timeout=...)` only kills the
    DIRECT child on TimeoutExpired -- the orphaned grandchild keeps the pipe open and
    `communicate()` blocks forever waiting for EOF that never comes. Reproduced here with a
    surrogate parent that spawns a long-sleeping grandchild inheriting its stdout, then hangs
    itself: with the fix (start_new_session + killpg), _run_raw must return promptly once its
    own `timeout` elapses, not wait out the grandchild's sleep."""
    parent_script = (
        "import subprocess, sys, time; "
        f"gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_GRANDCHILD_HANG_S})']); "
        "sys.stdout.flush(); "
        f"time.sleep({_GRANDCHILD_HANG_S})"
    )
    t0 = time.time()
    _run_raw([sys.executable, "-c", parent_script], timeout=2)
    elapsed = time.time() - t0
    assert elapsed < _GRANDCHILD_HANG_S, (
        f"_run_raw took {elapsed:.1f}s for a timeout=2 call -- the grandchild's stdout pipe "
        f"was not closed, so communicate() waited out its {_GRANDCHILD_HANG_S}s sleep instead "
        f"of returning shortly after the timeout"
    )


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
