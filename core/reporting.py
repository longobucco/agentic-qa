"""Generic reporting: pass-rate, per-bucket breakdown, and mean±std across runs.

Reads the run-dir layout written by `core.results`. LLM-judged benchmarks are
nondeterministic in BOTH the agent and the judge, so a single number is misleading and a
harness-A/B delta read off one run is inside the noise. This reports:

  - pooled success rate over every run-eval (the headline),
  - **agent variance**: the per-run-index pass rate as mean ± std across runs,
  - **judge variance**: the average judge disagreement, when the judge was repeated,

so a delta between two harness variants can be called real only once it clears the spread.
"""
import json
from statistics import mean, pstdev


def _bucket_of_id(task_id):
    return task_id.split("--")[0]


def collect(results_dir, system):
    """Return {task_id: {"bucket": str, "runs": [run, ...]}} for one system.

    Each run is {"idx": int, "verdict", "reason", "answer", "judge_rate"|None}.
    """
    base = results_dir / system
    tasks = {}
    if not base.exists():
        return tasks
    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        bucket = _bucket_of_id(tdir.name)
        runs = []
        for rdir in sorted(p for p in tdir.iterdir()
                           if p.is_dir() and p.name.startswith("run_")):
            ev = rdir / "eval.json"
            if not ev.exists():
                continue
            try:
                e = json.loads(ev.read_text())
            except Exception:
                continue
            answer = ""
            res = rdir / "result.json"
            if res.exists():
                try:
                    r = json.loads(res.read_text())
                    answer = r.get("answer", "")
                    bucket = r.get("bucket") or bucket
                except Exception:
                    pass
            try:
                idx = int(rdir.name.split("_", 1)[1])
            except (ValueError, IndexError):
                idx = len(runs) + 1
            runs.append({
                "idx": idx,
                "verdict": e.get("verdict"),
                "reason": e.get("reason", ""),
                "answer": answer,
                "judge_rate": e.get("judge_success_rate"),
            })
        if runs:
            tasks[tdir.name] = {"bucket": bucket, "runs": runs}
    return tasks


def _ok(run):
    return run["verdict"] == "SUCCESS"


def summarize(results_dir, system, *, title="Benchmark"):
    tasks = collect(results_dir, system)
    if not tasks:
        print(f"=== {title} [{system}] ===\nNo evaluated tasks yet.")
        return

    all_runs = [r for t in tasks.values() for r in t["runs"]]
    n_runs = len(all_runs)
    ok_runs = sum(1 for r in all_runs if _ok(r))
    print(f"=== {title} [{system}] ===")
    print(f"Tasks: {len(tasks)}   Run-evals: {n_runs}   "
          f"Success: {ok_runs}   Failure: {n_runs - ok_runs}   "
          f"Rate: {100 * ok_runs / n_runs:.1f}%")

    # Agent variance: pass rate per run index, then mean±std across indices.
    by_index = {}
    for r in all_runs:
        by_index.setdefault(r["idx"], []).append(_ok(r))
    index_rates = [mean(v) for _, v in sorted(by_index.items()) if v]
    if len(index_rates) > 1:
        m, s = 100 * mean(index_rates), 100 * pstdev(index_rates)
        print(f"Agent variance: {m:.1f}% ± {s:.1f}%  across {len(index_rates)} runs/task")

    # Judge variance: average minority fraction over repeatedly-judged trajectories.
    judged = [r["judge_rate"] for r in all_runs if r["judge_rate"] is not None]
    if judged:
        disagreement = mean(min(rate, 1 - rate) for rate in judged)
        print(f"Judge variance: {100 * disagreement:.1f}% mean disagreement "
              f"over {len(judged)} repeatedly-judged trajectories")

    # Per-bucket pooled rate.
    buckets = {}
    for t in tasks.values():
        b = buckets.setdefault(t["bucket"], [0, 0])
        for r in t["runs"]:
            b[0] += 1
            b[1] += 1 if _ok(r) else 0
    print("\nPer-bucket:")
    for b in sorted(buckets):
        tot, good = buckets[b]
        print(f"  {b:<16} {good}/{tot}  ({100 * good / tot:.0f}%)")

    # Failed tasks: those that failed the majority of their runs.
    fails = []
    for tid, t in sorted(tasks.items()):
        good = sum(1 for r in t["runs"] if _ok(r))
        tot = len(t["runs"])
        if good * 2 <= tot:
            reason = next((r["reason"] for r in t["runs"] if not _ok(r)), "")
            fails.append((tid, good, tot, reason))
    print(f"\nFailed tasks ({len(fails)}):")
    for tid, good, tot, reason in fails:
        suffix = f" [{good}/{tot} runs ok]" if tot > 1 else ""
        print(f"  - {tid}{suffix}: {reason}")
