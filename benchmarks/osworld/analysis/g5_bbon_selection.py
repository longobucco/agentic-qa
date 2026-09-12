"""G5 idea #16 -- comparative Best-of-N trajectory selection, offline re-derivation from raw
(docs/ideas-to-explore.md). Direct adaptation of "Behavior Best-of-N" (bBoN, arXiv:2510.02250,
69.9% on OSWorld with GPT-5, vs 62.6% single-rollout) to this project's own 3-runs-per-task
Sonnet-5 campaign -- a fully general mechanism, not a per-app/per-file-type tool
(g5_verifier_check.py's inspect_pptx_text_colors/inspect_thunderbird_prefs approach hit a wall
on 3 persistent false positives; this asks a structurally different question).

Key methodological choice vs. arm #12 (g5_verifier_majority_vote.py): #12 asks 3 INDEPENDENT
verifier calls "is this one DONE or FAIL", then majority-votes the labels. bBoN's own ablation
(their Figure 5) reports comparative selection beats independent per-candidate scoring -- so
this script shows all 3 candidates to ONE judge call at once and asks it to pick the single best
trajectory, the same comparative-MCQ shape bBoN uses, rather than voting on 3 separate verdicts.

Lighter than bBoN's own "behavior narrative" (a VLM-generated fact log for every action,
built from before/after screenshots): this project doesn't save a screenshot per action, only
each run's final.png plus its own self-reported answer and turn count. The candidate summary
here is therefore end-state-plus-coarse-trajectory-stats, not a full step-by-step narrative --
an explicit, honest downgrade from bBoN's own mechanism, not a claim of parity with it.

Offline, no new sandbox: every completed run already has final.png, result.json, eval.json on
disk. Restricted to FLAKY tasks (g8_failure_taxonomy's definition): on an always-pass or
always-fail task, no selection policy can change the outcome, so those tasks are not where a
selection mechanism's value would show up.

Run: python -m benchmarks.osworld.analysis.g5_bbon_selection [--json]
"""
import json
import re
import sys
from pathlib import Path

from core.agent_loop import build_claude_cmd, extract_answer, run_claude_meta
from benchmarks.osworld import config
from benchmarks.osworld.analysis.g8_failure_taxonomy import taxonomy
from benchmarks.osworld.tasks import load_tasks

SELECTION_PROMPT = """You are comparing {n} independent attempts at the SAME task, made by an \
autonomous computer-use agent. You did not perform the task and have no memory of any attempt.

Task instruction: {instruction}

For each attempt you are given: its final desktop screenshot (read the exact absolute path \
given -- do not search for anything else), the agent's own final answer, and how many actions \
it took to get there.

{candidates}

Compare the {n} final screenshots directly against each other and against the task instruction. \
Pick the ONE attempt whose final state most plausibly satisfies the task. Do not default to the \
first one you saw be reasonable-looking -- actively look for a concrete way in which one \
candidate's state is more correct than the others', or a concrete flaw that rules one out.

Answer with exactly two lines:
ANSWER: <the run number of your pick, e.g. 1, 2, or 3>
REASON: one sentence naming the specific, concrete detail that decided it."""

_ANSWER_INT_RE = re.compile(r"^ANSWER:\s*(\d+)", re.MULTILINE)
_REASON_RE = re.compile(r"^REASON:\s*(.*)$", re.MULTILINE)


def _served_by(meta):
    served = [m for m in (meta.get("modelUsage") or {}) if not m.startswith("claude-haiku")]
    return sorted(served)


def select(task, candidates, *, timeout=120):
    """candidates: list of {"run": n, "final_png": Path, "answer": str, "turns": int}. Returns
    {"picked_run", "reason", "raw", "model_served"} -- "picked_run" is None if the model's
    answer didn't parse to a valid 1..N choice (never an exception)."""
    blocks = []
    for c in candidates:
        blocks.append(
            f"Attempt {c['run']}: screenshot at {c['final_png'].resolve()} -- "
            f"agent's own final answer: {c['answer']!r} ({c['turns']} actions taken)"
        )
    prompt = SELECTION_PROMPT.format(
        n=len(candidates), instruction=task.get("instruction", ""),
        candidates="\n".join(blocks),
    )
    cmd = build_claude_cmd(prompt, max_turns=len(candidates) + 2, allowed_tools=["Read"],
                           model=config.MODEL or None)
    meta = run_claude_meta(cmd, timeout=timeout)
    text = meta.get("result", "")
    m = _ANSWER_INT_RE.search(text)
    picked = int(m.group(1)) if m else None
    valid_runs = {c["run"] for c in candidates}
    if picked not in valid_runs:
        picked = None
    r = _REASON_RE.search(text)
    return {"picked_run": picked, "reason": r.group(1).strip() if r else None,
            "raw": text[:500], "model_served": _served_by(meta)}


def run(results_dir=None, system=None):
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    tasks = {t["id"]: t for t in load_tasks()}
    flaky = taxonomy(results_dir, system)["stability"]["flaky"]

    out = []
    for tid in flaky:
        task = tasks.get(tid, {})
        tdir = base / tid
        candidates, verdicts = [], {}
        for n in (1, 2, 3):
            rdir = tdir / f"run_{n}"
            png = rdir / "final.png"
            if not png.exists():
                continue
            try:
                res = json.loads((rdir / "result.json").read_text())
                ev = json.loads((rdir / "eval.json").read_text())
            except Exception:
                continue
            candidates.append({"run": n, "final_png": png,
                              "answer": (res.get("answer") or "").strip(),
                              "turns": res.get("agent_num_turns")})
            verdicts[n] = ev.get("verdict")
        if len(candidates) < 2:
            continue

        sel = select(task, candidates)
        picked_verdict = verdicts.get(sel["picked_run"]) if sel["picked_run"] else None
        # ground truth: majority of the official verdicts actually observed (same convention as
        # arm #12) -- what a perfect selector would be trying to hit.
        from collections import Counter
        gt_majority = Counter(verdicts.values()).most_common(1)[0][0]
        row = {
            "task": tid,
            "n_candidates": len(candidates),
            "verdicts_by_run": verdicts,
            "ground_truth_majority": gt_majority,
            "picked_run": sel["picked_run"],
            "picked_verdict": picked_verdict,
            "picked_matches_gt_majority": (
                picked_verdict == gt_majority if picked_verdict is not None else None),
            "reason": sel["reason"],
            "model_served": sel["model_served"],
            # reference baselines, computed from data already on disk, no extra cost:
            "run1_verdict": verdicts.get(1),   # "always take the first attempt" baseline
            "any_success": "SUCCESS" in verdicts.values(),   # oracle upper bound
        }
        out.append(row)
        print(f"{tid[:8]} verdicts={verdicts} picked=run_{sel['picked_run']}"
              f"({picked_verdict}) gt_majority={gt_majority}", file=sys.stderr)
    return out


def _summarize(rows):
    scored = [r for r in rows if r["picked_matches_gt_majority"] is not None]
    picked_right = sum(1 for r in scored if r["picked_matches_gt_majority"])
    run1_right = sum(1 for r in rows if r["run1_verdict"] == r["ground_truth_majority"])
    any_success_rate = sum(1 for r in rows if r["any_success"]) / len(rows) if rows else 0

    print(f"\n=== flaky tasks: {len(rows)}, selection parsed cleanly: {len(scored)} ===")
    print(f"bBoN-style comparative selection matches ground-truth majority: "
          f"{picked_right}/{len(scored)} ({100*picked_right/len(scored):.1f}%)" if scored else "n/a")
    print(f"'always take run_1' baseline matches ground-truth majority: "
          f"{run1_right}/{len(rows)} ({100*run1_right/len(rows):.1f}%)" if rows else "n/a")
    print(f"oracle upper bound (>=1 of 3 runs is SUCCESS): "
          f"{100*any_success_rate:.1f}% of flaky tasks")


def _main():
    rows = run(system=config.system_from_argv())
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return
    _summarize(rows)


if __name__ == "__main__":
    _main()
