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


# ---- harness A/B: compare two systems on the SAME tasks, with significance --------------

def _harness_record(results_dir, system):
    """Read the pinned-config record `core.run` wrote for a system ({} if absent)."""
    p = results_dir / system / "harness.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _agent_stats(tasks):
    """Pooled rate + agent variance (mean±std of pass rate across run indices) for a task set."""
    all_runs = [r for t in tasks.values() for r in t["runs"]]
    n = len(all_runs)
    ok = sum(1 for r in all_runs if _ok(r))
    by_index = {}
    for r in all_runs:
        by_index.setdefault(r["idx"], []).append(_ok(r))
    idx_rates = [mean(v) for _, v in sorted(by_index.items()) if v]
    m = mean(idx_rates) if idx_rates else (ok / n if n else 0.0)
    s = pstdev(idx_rates) if len(idx_rates) > 1 else 0.0
    return {"n_runs": n, "ok": ok, "mean": m, "std": s, "single_run": len(idx_rates) <= 1}


def _bucket_rates(tasks):
    out = {}
    for t in tasks.values():
        tot, good = out.get(t["bucket"], (0, 0))
        for r in t["runs"]:
            tot += 1
            good += 1 if _ok(r) else 0
        out[t["bucket"]] = (tot, good)
    return out


def ab_compare(results_dir, system_a, system_b, *, title="Benchmark"):
    """Harness A/B: report system_b − system_a on the tasks BOTH evaluated, and whether the
    delta clears the combined agent-variance spread (the ticket's "call a delta real only once
    it clears mean±std"). Also audits the pinned config so the A/B isn't silently confounded by
    a different model/budget between the two harnesses."""
    a_all = collect(results_dir, system_a)
    b_all = collect(results_dir, system_b)
    print(f"=== {title}: harness A/B — {system_b} vs {system_a} ===")

    # Pinning audit: the comparison is only "harness, not model" if model/budget match.
    ha, hb = _harness_record(results_dir, system_a), _harness_record(results_dir, system_b)
    if ha or hb:
        print("Pinned config (must match for a clean harness A/B):")
        print(f"  {system_a}: env={ha.get('env', {})} runs={ha.get('runs')}")
        print(f"  {system_b}: env={hb.get('env', {})} runs={hb.get('runs')}")
        if ha.get("env") != hb.get("env"):
            print("  ⚠ configs differ — pin the SAME model/budget across both before trusting Δ.")

    common = sorted(set(a_all) & set(b_all))
    if not common:
        print(f"\nNo overlapping tasks yet: {system_a} has {len(a_all)}, {system_b} has "
              f"{len(b_all)}. Run the SAME task subset through both, then compare.")
        return
    a = {k: a_all[k] for k in common}
    b = {k: b_all[k] for k in common}
    sa, sb = _agent_stats(a), _agent_stats(b)
    print(f"\nCommon tasks: {len(common)}")
    print(f"  {system_a:<14} {100 * sa['mean']:5.1f}% ± {100 * sa['std']:.1f}%   "
          f"({sa['ok']}/{sa['n_runs']} run-evals)")
    print(f"  {system_b:<14} {100 * sb['mean']:5.1f}% ± {100 * sb['std']:.1f}%   "
          f"({sb['ok']}/{sb['n_runs']} run-evals)")
    delta = 100 * (sb["mean"] - sa["mean"])
    spread = 100 * ((sa["std"] ** 2 + sb["std"] ** 2) ** 0.5)
    if sa["single_run"] and sb["single_run"]:
        note = "single run/task — re-roll with --runs N to bound agent variance before trusting Δ"
    elif abs(delta) > spread:
        note = "REAL — clears the combined mean±std spread"
    else:
        note = "NOT significant — within the mean±std spread"
    print(f"  Δ = {delta:+.1f}%   (spread ±{spread:.1f}%)  ->  {note}")

    # Per-bucket side-by-side.
    ra, rb = _bucket_rates(a), _bucket_rates(b)
    print("\nPer-bucket (Δ = B − A):")
    for bk in sorted(set(ra) | set(rb)):
        ta, ga = ra.get(bk, (0, 0))
        tb, gb = rb.get(bk, (0, 0))
        pa = 100 * ga / ta if ta else 0.0
        pb = 100 * gb / tb if tb else 0.0
        print(f"  {bk:<16} {system_a}={pa:5.1f}% ({ga}/{ta})   "
              f"{system_b}={pb:5.1f}% ({gb}/{tb})   Δ={pb - pa:+.1f}%")
