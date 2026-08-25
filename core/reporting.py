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

from core.results import is_run_dir


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
                           if p.is_dir() and is_run_dir(p.name)):
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


_INCONCLUSIVE_VERDICTS = {
    "ENVIRONMENT_ERROR",  # task's environment never prepared correctly (e.g. OSWorld config
                          # setup failed) — the agent was never meaningfully run
    "EVAL_ERROR",         # agent ran, but scoring itself failed for a reason unrelated to the
                          # agent's behavior (e.g. a transient network error mid-evaluation) —
                          # forcing this into FAILURE would contaminate agent-reliability
                          # numbers with our own pipeline's flakiness
}


def _env_error(run):
    """Neither evidence of agent success nor failure — excluded from pass-rate, variance, and
    per-bucket stats below. Reported as its own count instead of silently dropped: either kind
    recurring is itself a finding."""
    return run["verdict"] in _INCONCLUSIVE_VERDICTS


def collect_infra_errors(results_dir, system):
    """{"task_id/run_k": [attempt, ...]} for runs that died before producing a verdict.

    Kept separate from `collect`: these never produced an eval.json (so resume retries
    them), so they're invisible to every pass-rate -- exactly the infra-flakiness term a
    reliability study needs to subtract out before calling instability a benchmark property."""
    base = results_dir / system
    out = {}
    if not base.exists():
        return out
    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        for rdir in sorted(p for p in tdir.iterdir()
                            if p.is_dir() and is_run_dir(p.name)):
            path = rdir / "infra_error.json"
            if not path.exists():
                continue
            try:
                out[f"{tdir.name}/{rdir.name}"] = json.loads(path.read_text())
            except Exception:
                continue
    return out


def summarize(results_dir, system, *, title="Benchmark"):
    tasks = collect(results_dir, system)
    if not tasks:
        print(f"=== {title} [{system}] ===\nNo evaluated tasks yet.")
        return

    all_runs = [r for t in tasks.values() for r in t["runs"]]
    env_errors = [r for r in all_runs if _env_error(r)]
    scored_runs = [r for r in all_runs if not _env_error(r)]
    n_runs = len(scored_runs)
    ok_runs = sum(1 for r in scored_runs if _ok(r))
    print(f"=== {title} [{system}] ===")
    if n_runs:
        print(f"Tasks: {len(tasks)}   Run-evals: {n_runs}   "
              f"Success: {ok_runs}   Failure: {n_runs - ok_runs}   "
              f"Rate: {100 * ok_runs / n_runs:.1f}%")
    else:
        print(f"Tasks: {len(tasks)}   Run-evals: 0 (all {len(env_errors)} run(s) were "
              f"environment/eval errors — no pass-rate to report)")
    if env_errors:
        by_verdict = {}
        for r in env_errors:
            by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        print(f"Excluded (inconclusive): {len(env_errors)} {by_verdict} — not evidence of "
              f"agent success or failure; see reasons below")

    infra = collect_infra_errors(results_dir, system)
    if infra:
        attempts = sum(len(v) for v in infra.values())
        kinds = {}
        for recs in infra.values():
            for r in recs:
                kinds[r.get("outcome", "?")] = kinds.get(r.get("outcome", "?"), 0) + 1
        print(f"Infrastructure failures: {attempts} attempt(s) across {len(infra)} run-dir(s) "
              f"{kinds} — never reached a verdict (provisioning/controller/harness). "
              f"Excluded from every number above.")

    # Agent variance: pass rate per run index, then mean±std across indices.
    by_index = {}
    for r in scored_runs:
        by_index.setdefault(r["idx"], []).append(_ok(r))
    index_rates = [mean(v) for _, v in sorted(by_index.items()) if v]
    if len(index_rates) > 1:
        m, s = 100 * mean(index_rates), 100 * pstdev(index_rates)
        print(f"Agent variance: {m:.1f}% ± {s:.1f}%  across {len(index_rates)} runs/task")

    # Judge variance: average minority fraction over repeatedly-judged trajectories.
    judged = [r["judge_rate"] for r in scored_runs if r["judge_rate"] is not None]
    if judged:
        disagreement = mean(min(rate, 1 - rate) for rate in judged)
        print(f"Judge variance: {100 * disagreement:.1f}% mean disagreement "
              f"over {len(judged)} repeatedly-judged trajectories")

    # Per-bucket pooled rate (environment errors excluded, same as the headline rate).
    buckets = {}
    for t in tasks.values():
        for r in t["runs"]:
            if _env_error(r):
                continue
            b = buckets.setdefault(t["bucket"], [0, 0])
            b[0] += 1
            b[1] += 1 if _ok(r) else 0
    if buckets:
        print("\nPer-bucket:")
        for b in sorted(buckets):
            tot, good = buckets[b]
            print(f"  {b:<16} {good}/{tot}  ({100 * good / tot:.0f}%)")

    # Failed tasks: those that failed the majority of their SCORED runs (env errors excluded).
    fails = []
    env_error_tasks = []
    for tid, t in sorted(tasks.items()):
        scored = [r for r in t["runs"] if not _env_error(r)]
        if not scored:
            reason = next((r["reason"] for r in t["runs"]), "")
            env_error_tasks.append((tid, len(t["runs"]), reason))
            continue
        good = sum(1 for r in scored if _ok(r))
        tot = len(scored)
        if good * 2 <= tot:
            reason = next((r["reason"] for r in scored if not _ok(r)), "")
            fails.append((tid, good, tot, reason))
    print(f"\nFailed tasks ({len(fails)}):")
    for tid, good, tot, reason in fails:
        suffix = f" [{good}/{tot} runs ok]" if tot > 1 else ""
        print(f"  - {tid}{suffix}: {reason}")
    if env_error_tasks:
        print(f"\nExcluded tasks ({len(env_error_tasks)}, every run inconclusive — "
              f"not scored either way):")
        for tid, n, reason in env_error_tasks:
            print(f"  - {tid} [{n} run(s)]: {reason}")


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
    """Pooled rate + agent variance (mean±std of pass rate across run indices) for a task set.
    Environment-error runs are excluded (see summarize()'s _env_error)."""
    all_runs = [r for t in tasks.values() for r in t["runs"] if not _env_error(r)]
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
            if _env_error(r):
                continue
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
