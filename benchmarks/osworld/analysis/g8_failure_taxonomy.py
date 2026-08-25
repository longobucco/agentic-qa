"""G8 -- failure taxonomy over COMPLETED tasks only, re-derived from raw, no new runs.

Thesis phase 2 ("identify the bugs and where the failures are"). Where G3/G4 ask *how many*
runs pass, this asks *what broke* on the ones that didn't, and at which layer:

    INFRA    the run never produced a usable verdict because the environment was never
             correctly prepared            -> ENVIRONMENT_ERROR
    ORACLE   the agent ran, but the scoring machinery could not deliver a verdict
             (upstream getter/metric crash, unroutable port, missing symbol)
                                           -> EVAL_ERROR
    AGENT    the agent ran and was scored, and lost -> FAILURE

Only *completed* tasks enter: every run directory on disk is scored AND there are at least
MIN_RUNS of them. Partially-run tasks are excluded because a task caught mid-campaign has a
verdict mix biased by run order (the campaign is serial), and never-run tasks obviously carry
no evidence. This matters concretely: the G3-full campaign writes into results/ while this
module reads it, so the cohort grows between invocations -- every number here is stamped with
the cohort size that produced it rather than quoted bare.

Reason clustering normalises volatile substrings (paths, uuids, digits) so the same upstream
crash on twelve different tasks collapses to one catalogue entry instead of twelve. Same idea
as g3_anomaly_scan's reason_cluster, but keyed on the exception tail rather than the whole
reason string, and applied to completed tasks only.

Run: python -m benchmarks.osworld.analysis.g8_failure_taxonomy [--json]
"""
import collections
import json
import re
import sys

from benchmarks.osworld import config
from benchmarks.osworld.tasks import load_tasks
from core.results import is_run_dir

MIN_RUNS = 3            # the campaign's N; a task with fewer scored runs is "partial"
TURN_CAP = 150          # config.MAX_TURNS at the time of the G3 campaign

INFRA_VERDICTS = {"ENVIRONMENT_ERROR"}
ORACLE_VERDICTS = {"EVAL_ERROR"}
AGENT_VERDICTS = {"SUCCESS", "FAILURE"}

# Metrics that compare a whole artifact against a gold file rather than checking the specific
# property the instruction asked for. Named here (not derived) because the distinction is
# semantic: the function name is the only reliable signal of "diffs everything".
WHOLE_ARTIFACT_METRICS = {
    "compare_pptx_files", "compare_docx_files", "compare_docx_files_and_ignore_new_lines",
    "compare_table", "compare_pdfs", "compare_images", "compare_audios", "compare_csv",
}


def _norm_reason(reason):
    """Collapse a verdict reason to the underlying failure signature.

    Keeps only the tail after 'official eval errored: ' (the rest is per-task eval_state that
    differs on every task and would defeat clustering), then blanks paths, uuids and numbers.
    """
    text = reason or ""
    tail = re.search(r"official eval errored:\s*(.*)$", text, re.S)
    key = tail.group(1) if tail else text
    key = re.sub(r"\s+", " ", key).strip()
    key = re.sub(r"'/[^']*'", "'<path>'", key)
    key = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", "<uuid>", key)
    key = re.sub(r"\d+", "N", key)
    return key.rstrip(") ").strip()[:200]


def _answer_kind(record):
    ans = ((record or {}).get("answer") or "").strip().upper()
    if ans.startswith("DONE"):
        return "DONE"
    if ans.startswith("FAIL"):
        return "FAIL"
    return "none"


def _funcs(task):
    f = (task.get("evaluator") or {}).get("func")
    return set(f) if isinstance(f, list) else ({f} if f else set())


def _app(task):
    apps = task.get("related_apps") or []
    return apps[0] if apps else "?"


def load_cohort(results_dir=None, system="agent_computer", min_runs=MIN_RUNS):
    """Split every task directory on disk into completed / partial / never_run.

    A run counts as scored only if eval.json carries a verdict; a run directory holding just
    result.json (agent ran, scoring never wrote) is unscored, and one holding only
    infra_error.json never ran at all. Both keep the task out of the completed cohort.
    """
    results_dir = results_dir or config.RESULTS_DIR
    base = results_dir / system
    tasks = {t["id"]: t for t in load_tasks()}
    cohorts = {"completed": {}, "partial": {}, "never_run": {}}
    if not base.exists():
        return cohorts, tasks

    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        runs = {}
        unscored = 0
        for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and is_run_dir(p.name)):
            try:
                ev = json.loads((rdir / "eval.json").read_text())
            except Exception:
                unscored += 1
                continue
            if not ev.get("verdict"):
                unscored += 1
                continue
            try:
                res = json.loads((rdir / "result.json").read_text())
            except Exception:
                res = {}
            runs[rdir.name] = {"eval": ev, "result": res}
        if len(runs) >= min_runs and not unscored:
            cohorts["completed"][tdir.name] = runs
        elif runs:
            cohorts["partial"][tdir.name] = runs
        else:
            cohorts["never_run"][tdir.name] = runs
    return cohorts, tasks


def _layer(verdict):
    if verdict in INFRA_VERDICTS:
        return "INFRA"
    if verdict in ORACLE_VERDICTS:
        return "ORACLE"
    return "AGENT"


def taxonomy(results_dir=None, system="agent_computer", min_runs=MIN_RUNS):
    """The whole report as plain data, so callers render it and tests assert on it."""
    cohorts, tasks = load_cohort(results_dir, system, min_runs)
    done = cohorts["completed"]

    layers = collections.Counter()
    verdicts = collections.Counter()
    per_app = collections.defaultdict(collections.Counter)
    clusters = collections.defaultdict(lambda: collections.defaultdict(set))
    cost_by_verdict = collections.Counter()
    partial_reward = []
    cap_hits = collections.defaultdict(list)
    done_but_failed = collections.Counter()
    done_but_failed_total = collections.Counter()
    infeasible_missed = {}
    flaky, always_pass, always_fail = [], [], []

    for tid, runs in done.items():
        task = tasks.get(tid, {})
        app = _app(task)
        seen = [r["eval"].get("verdict") for r in runs.values()]

        for r in runs.values():
            v = r["eval"].get("verdict")
            reason = r["eval"].get("reason") or ""
            verdicts[v] += 1
            layers[_layer(v)] += 1
            per_app[app][v] += 1
            cost_by_verdict[v] += (r["result"] or {}).get("agent_cost_usd") or 0.0

            if v in INFRA_VERDICTS | ORACLE_VERDICTS:
                clusters[v][_norm_reason(reason)].add(tid)

            if v in AGENT_VERDICTS:
                turns = (r["result"] or {}).get("agent_num_turns") or 0
                if turns >= TURN_CAP:
                    cap_hits[tid].append(v)
                kind = _answer_kind(r["result"])
                done_but_failed_total[app] += 1
                if v == "FAILURE" and kind == "DONE":
                    done_but_failed[app] += 1
                reward = re.search(r"->\s*([0-9]*\.[0-9]+)\s*$", reason)
                if reward and reward.group(1) not in ("0.00", "1.00"):
                    partial_reward.append({"task": tid, "app": app,
                                           "reward": float(reward.group(1)), "verdict": v})

        # infeasibility recognition: the task is impossible, did the agent say so?
        if "infeasible" in _funcs(task):
            kinds = collections.Counter(_answer_kind(r["result"]) for r in runs.values())
            infeasible_missed[tid] = {"app": app, "answers": dict(kinds),
                                      "verdicts": dict(collections.Counter(seen))}

        if set(seen) <= AGENT_VERDICTS:
            if len(set(seen)) > 1:
                flaky.append(tid)
            elif "SUCCESS" in set(seen):
                always_pass.append(tid)
            else:
                always_fail.append(tid)

    scored = verdicts["SUCCESS"] + verdicts["FAILURE"]
    return {
        "cohort": {k: len(v) for k, v in cohorts.items()},
        "n_runs": sum(verdicts.values()),
        "layers": dict(layers),
        "verdicts": dict(verdicts),
        "pass_rate": round(verdicts["SUCCESS"] / scored, 4) if scored else None,
        "per_app": {a: dict(c) for a, c in sorted(per_app.items())},
        "clusters": {v: sorted(((sorted(ids), k) for k, ids in c.items()),
                               key=lambda x: -len(x[0]))
                     for v, c in clusters.items()},
        "cost_by_verdict": {v: round(c, 2) for v, c in cost_by_verdict.items()},
        "wasted_usd": round(cost_by_verdict["EVAL_ERROR"]
                            + cost_by_verdict["ENVIRONMENT_ERROR"], 2),
        "partial_reward": sorted(partial_reward, key=lambda r: r["reward"]),
        "turn_cap_hits": {t: v for t, v in sorted(cap_hits.items(), key=lambda x: -len(x[1]))},
        "done_but_failed": {a: {"n": done_but_failed[a], "of": done_but_failed_total[a]}
                            for a in sorted(done_but_failed_total,
                                            key=lambda a: -(done_but_failed[a]
                                                            / max(done_but_failed_total[a], 1)))},
        "infeasible": infeasible_missed,
        "stability": {"flaky": sorted(flaky), "always_pass": sorted(always_pass),
                      "always_fail": sorted(always_fail)},
        "whole_artifact_tasks": sorted(t for t in done
                                       if _funcs(tasks.get(t, {})) & WHOLE_ARTIFACT_METRICS),
    }


def _bar(label, count, total, width=28):
    filled = int(width * count / total) if total else 0
    return f"  {label:20s} {count:5d}  {'#' * filled}{'.' * (width - filled)}  {100*count/total:5.1f}%"


def _main():
    report = taxonomy()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return

    c = report["cohort"]
    print(f"COHORT  completed {c['completed']}  partial {c['partial']}  never_run {c['never_run']}"
          f"   ({report['n_runs']} scored runs; completed tasks only from here)\n")

    print("FAILURE LAYER")
    for k in ("AGENT", "ORACLE", "INFRA"):
        print(_bar(k, report["layers"].get(k, 0), report["n_runs"]))
    print(f"\n  pass-rate over scored agent runs: {100 * report['pass_rate']:.1f}%"
          f"   agent cost sunk into unscorable runs: ${report['wasted_usd']}\n")

    print("PER APP".ljust(24) + "runs  SUCC  FAIL  EVAL_E  ENV_E   pass%")
    for app, v in sorted(report["per_app"].items(),
                         key=lambda x: -sum(x[1].values())):
        n = sum(v.values())
        s, f = v.get("SUCCESS", 0), v.get("FAILURE", 0)
        rate = f"{100*s/(s+f):.1f}" if s + f else "n/a"
        print(f"  {app:20s}{n:5d}{s:6d}{f:6d}{v.get('EVAL_ERROR',0):8d}"
              f"{v.get('ENVIRONMENT_ERROR',0):7d}{rate:>8}")

    for verdict, entries in report["clusters"].items():
        print(f"\n{verdict} — bug catalogue ({len(entries)} distinct signatures)")
        for ids, sig in entries:
            print(f"  [{len(ids):2d} tasks] {sig}")
            print(f"             {', '.join(i[:8] for i in ids)}")

    st = report["stability"]
    n = len(st["flaky"]) + len(st["always_pass"]) + len(st["always_fail"])
    print(f"\nSTABILITY  always-pass {len(st['always_pass'])}  always-fail {len(st['always_fail'])}"
          f"  FLAKY {len(st['flaky'])} ({100*len(st['flaky'])/n:.1f}% of {n} agent-scored tasks)")

    print("\nAGENT BELIEVED IT FINISHED BUT THE ORACLE SAID NO")
    for app, d in report["done_but_failed"].items():
        print(f"  {app:20s} {d['n']:4d}/{d['of']:4d} = {100*d['n']/max(d['of'],1):5.1f}%")

    if report["partial_reward"]:
        print(f"\nPARTIAL CREDIT DISCARDED BY BINARY SCORING ({len(report['partial_reward'])} runs)")
        for r in report["partial_reward"]:
            print(f"  {r['task'][:8]} {r['app']:20s} reward={r['reward']:.2f} -> {r['verdict']}")

    missed = {t: d for t, d in report["infeasible"].items() if d["answers"].get("FAIL", 0) == 0}
    print(f"\nINFEASIBLE TASKS  {len(report['infeasible'])} completed, "
          f"{len(missed)} never once declared FAIL by the agent")
    for t, d in sorted(missed.items()):
        print(f"  {t[:8]} {d['app']:20s} answers={d['answers']}")


if __name__ == "__main__":
    _main()
