"""G5 idea #9 -- independent verifier check, offline re-derivation (docs/g5-arm-independent-
verifier-plan.md). Follows ARM_SELF_VERIFY's clean negative result (2026-08-29, 12/12 runs,
same-agent re-observation changed nothing): does an INDEPENDENT check -- no memory of the
attempt, just the instruction and the final screenshot -- catch what self-verification missed?

No new sandbox, no MCP, no agent_computer run: every completed run already has final.png on
disk (tracked by .gitignore's own allowlist -- "cheap and high diagnostic value on its own").
One single-turn `claude -p` call per run, `--allowedTools Read` only, judges DONE/FAIL from the
screenshot alone.

Mechanism note (found live during design, 2026-08-29): an early version of this prompt said
"the file in the current directory" and got a 16-turn, $0.18 run where the agent used Bash
(find/grep/cat/ls across the whole repo) instead of Read, despite --allowedTools Read --
independent confirmation of the artifact's own sandbox-escape finding (--dangerously-skip-
permissions doesn't block tools outside --allowedTools, only suppresses the prompt). Fixed by
giving the exact absolute path and an explicit "do not search" instruction; every call since
completes in 2 turns for a few cents.

Run: python -m benchmarks.osworld.analysis.g5_verifier_check [--json]
"""
import json
import sys
from pathlib import Path

from core.agent_loop import build_claude_cmd, extract_answer, run_claude
from benchmarks.osworld import config
from benchmarks.osworld.tasks import load_tasks

VERIFIER_PROMPT = """Read the image file at the EXACT absolute path: {path} -- it is a \
screenshot of a Linux desktop, taken at the end of an attempt to complete a task. Do not \
search for anything else; the path given is correct and complete.

The task instruction was: {instruction}

Based ONLY on what you see in the screenshot, does the desktop state satisfy this \
instruction? Do not assume steps were taken that aren't visible in the image. Answer with \
exactly one line: ANSWER: DONE or ANSWER: FAIL"""

# The 4 ARM_SELF_VERIFY tasks, both conditions (run_N = self-verify, run_N_g3baseline_legacy =
# baseline) -- 4 tasks x 3 runs x 2 conditions = 24 targets.
TARGETS = [
    "3f49d2cc-f400-4e7d-90cc-9b18e401cc31",
    "e2b5e914-ffe1-44d2-8e92-58f8c5d92bb2",
    "045bf3ff-9077-4b86-b483-a1040a949cff",
    "6d72aad6-187a-4392-a4c4-ed87269c51cf",
]


def verify(final_png, instruction, *, timeout=120):
    """One independent verifier call. Returns {"answer": "DONE"|"FAIL"|"", "raw": str}."""
    prompt = VERIFIER_PROMPT.format(path=final_png, instruction=instruction)
    cmd = build_claude_cmd(prompt, max_turns=4, allowed_tools=["Read"])
    text = run_claude(cmd, timeout=timeout)
    return {"answer": extract_answer(text), "raw": text}


def run(results_dir=None, system=None):
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    tasks = {t["id"]: t for t in load_tasks()}
    out = []

    for tid in TARGETS:
        task = tasks.get(tid, {})
        instruction = task.get("instruction", "")
        func = (task.get("evaluator") or {}).get("func")
        func_set = set(func) if isinstance(func, list) else ({func} if func else set())
        is_infeasible = "infeasible" in func_set
        for n in (1, 2, 3):
            for cond, subdir in (("self_verify", f"run_{n}"), ("baseline", f"run_{n}_g3baseline_legacy")):
                rdir = base / tid / subdir
                png = rdir / "final.png"
                if not png.exists():
                    continue
                try:
                    result = json.loads((rdir / "result.json").read_text())
                    ev = json.loads((rdir / "eval.json").read_text())
                except Exception:
                    continue
                v = verify(png.resolve(), instruction)
                row = {
                    "task": tid, "run": n, "condition": cond, "is_infeasible": is_infeasible,
                    "agent_answer": (result.get("answer") or "").strip(),
                    "official_verdict": ev.get("verdict"),
                    "verifier_answer": v["answer"],
                }
                row["verifier_matches_official"] = _matches(row["verifier_answer"], row["official_verdict"], is_infeasible)
                row["agent_matches_official"] = _matches(row["agent_answer"], row["official_verdict"], is_infeasible)
                out.append(row)
                print(f"{tid[:8]} run_{n} {cond:12s} agent={row['agent_answer']:5s} "
                      f"verifier={row['verifier_answer']:5s} official={row['official_verdict']}")
    return out


def _matches(answer, official_verdict, is_infeasible):
    """Does the answer's implicit claim agree with the true outcome? For an ordinary task,
    DONE claims success (agrees with SUCCESS) and FAIL claims failure (agrees with FAILURE).
    For an infeasible-class task the evaluator's own convention inverts this: a *correct* FAIL
    (the agent rightly recognized the task as impossible) is scored SUCCESS, and an incorrect
    DONE is scored FAILURE -- so the DONE/FAIL <-> SUCCESS/FAILURE mapping flips for this class,
    same distinction g8_failure_taxonomy.py's infeasibility tracking already makes."""
    ans = (answer or "").strip().upper()
    if is_infeasible:
        if ans.startswith("FAIL"):
            return official_verdict == "SUCCESS"
        if ans.startswith("DONE"):
            return official_verdict != "SUCCESS"
        return None
    if ans.startswith("DONE"):
        return official_verdict == "SUCCESS"
    if ans.startswith("FAIL"):
        return official_verdict != "SUCCESS"
    return None


def _main():
    rows = run(system=config.system_from_argv())
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return
    n = len(rows)
    verifier_right = sum(1 for r in rows if r["verifier_matches_official"])
    agent_right = sum(1 for r in rows if r["agent_matches_official"])
    print(f"\n{n} verifier calls -- verifier matched official verdict {verifier_right}/{n}, "
          f"agent's own answer matched official verdict {agent_right}/{n}")


if __name__ == "__main__":
    _main()
