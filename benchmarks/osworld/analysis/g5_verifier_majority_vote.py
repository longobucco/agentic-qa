"""G5 idea #12 -- independent-verifier majority vote against the flaky-task set (docs/
g5-arm-verifier-majority-vote-plan.md). Direct extension of idea #9's mechanism
(g5_verifier_check.py): reuses its verify() and _matches() unchanged, applied to all 3
final.png of each G8-flaky task instead of the 4-task ARM_SELF_VERIFY set.

RQ: a flaky task's 3 identically-instructed baseline runs disagree on the official verdict
(always a 2-1 split, by G8's own definition of "flaky"). Does #9's independent verifier,
majority-voted across all 3 final.png, converge on that 2-1 ground-truth majority more often
than a single real run's own self-report would (the practical N=1 situation this campaign's
harness is normally in)?

Offline, no new sandbox: every completed run already has final.png and eval.json on disk.

Run: python -m benchmarks.osworld.analysis.g5_verifier_majority_vote [--json]
"""
import collections
import json
import sys

from benchmarks.osworld import config
from benchmarks.osworld.analysis.g5_verifier_check import verify, _matches
from benchmarks.osworld.analysis.g8_failure_taxonomy import taxonomy
from benchmarks.osworld.tasks import load_tasks


def _funcs(task):
    f = (task.get("evaluator") or {}).get("func")
    return set(f) if isinstance(f, list) else ({f} if f else set())


def _implied(answer, is_infeasible):
    """What SUCCESS/FAILURE does this DONE/FAIL answer claim, standing alone -- same
    infeasible-class inversion as g5_verifier_check._matches, but returning the implied
    verdict itself rather than a match/no-match boolean against a specific reference."""
    ans = (answer or "").strip().upper()
    if is_infeasible:
        if ans.startswith("FAIL"):
            return "SUCCESS"
        if ans.startswith("DONE"):
            return "FAILURE"
        return None
    if ans.startswith("DONE"):
        return "SUCCESS"
    if ans.startswith("FAIL"):
        return "FAILURE"
    return None


def _majority(verdicts):
    """Majority of a list of SUCCESS/FAILURE/None. TIE if evenly split, None if no clean
    answers at all."""
    clean = [v for v in verdicts if v]
    if not clean:
        return None
    ranked = collections.Counter(clean).most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "TIE"
    return ranked[0][0]


def run(results_dir=None, system=None):
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    tasks = {t["id"]: t for t in load_tasks()}
    flaky = taxonomy(results_dir, system)["stability"]["flaky"]

    out = []
    for tid in flaky:
        task = tasks.get(tid, {})
        instruction = task.get("instruction", "")
        is_infeasible = "infeasible" in _funcs(task)
        tdir = base / tid

        official = []
        agent_matches = []
        verifier_calls = []
        for n in (1, 2, 3):
            rdir = tdir / f"run_{n}"
            try:
                ev = json.loads((rdir / "eval.json").read_text())
                res = json.loads((rdir / "result.json").read_text())
            except Exception:
                continue
            v = ev.get("verdict")
            official.append(v)
            agent_answer = (res.get("answer") or "").strip()

            png = rdir / "final.png"
            verifier_answer = None
            if png.exists():
                vr = verify(png.resolve(), instruction)
                verifier_answer = vr["answer"]
                verifier_calls.append({"run": n, "answer": verifier_answer, "raw": vr["raw"][:200]})
                print(f"{tid[:8]} run_{n} official={v:8s} agent={agent_answer:5s} "
                      f"verifier={verifier_answer}", file=sys.stderr)
            else:
                print(f"{tid[:8]} run_{n} official={v:8s} agent={agent_answer:5s} "
                      f"verifier=<no final.png>", file=sys.stderr)

            agent_matches.append({
                "run": n, "answer": agent_answer,
                "matches_official": _matches(agent_answer, v, is_infeasible),
            })

        gt_majority = _majority(official)  # always a clean 2-1 split by construction of "flaky"
        verifier_majority = _majority(_implied(c["answer"], is_infeasible) for c in verifier_calls)

        row = {
            "task": tid, "is_infeasible": is_infeasible,
            "official_verdicts": official,
            "ground_truth_majority": gt_majority,
            "n_verifier_calls": len(verifier_calls),
            "verifier_calls": verifier_calls,
            "verifier_majority": verifier_majority,
            "verifier_majority_matches_gt": (
                verifier_majority == gt_majority if verifier_majority not in (None, "TIE") else None
            ),
            "agent_runs": agent_matches,
            "agent_matches_gt_majority": [
                {"run": a["run"], "answer": a["answer"],
                 "matches": _matches(a["answer"], gt_majority, is_infeasible)}
                for a in agent_matches
            ],
        }
        out.append(row)
    return out


def _summarize(rows):
    non_infeasible = [r for r in rows if not r["is_infeasible"]]
    infeasible = [r for r in rows if r["is_infeasible"]]

    def _vm_rate(subset):
        scored = [r for r in subset if r["verifier_majority_matches_gt"] is not None]
        n_right = sum(1 for r in scored if r["verifier_majority_matches_gt"])
        return n_right, len(scored)

    def _agent_pool_rate(subset):
        pool = [a for r in subset for a in r["agent_matches_gt_majority"] if a["matches"] is not None]
        n_right = sum(1 for a in pool if a["matches"])
        return n_right, len(pool)

    vm_right, vm_n = _vm_rate(non_infeasible)
    ag_right, ag_n = _agent_pool_rate(non_infeasible)
    ties = sum(1 for r in non_infeasible if r["verifier_majority"] == "TIE")

    print(f"\n=== non-infeasible flaky tasks: {len(non_infeasible)} ===")
    print(f"verifier majority matches ground-truth majority: {vm_right}/{vm_n} "
          f"({100*vm_right/vm_n:.1f}%)" if vm_n else "verifier majority: n/a")
    print(f"agent single-run self-report matches ground-truth majority (pooled): "
          f"{ag_right}/{ag_n} ({100*ag_right/ag_n:.1f}%)" if ag_n else "agent pool: n/a")
    print(f"verifier majority ties (insufficient/split screenshots): {ties}")

    if infeasible:
        vm_right_i, vm_n_i = _vm_rate(infeasible)
        print(f"\n=== infeasible-class flaky tasks: {len(infeasible)} "
              f"(non-comparable caveat -- see plan doc) ===")
        print(f"verifier majority matches ground-truth majority: {vm_right_i}/{vm_n_i}")


def _main():
    rows = run(system=config.system_from_argv())
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return
    _summarize(rows)


if __name__ == "__main__":
    _main()
