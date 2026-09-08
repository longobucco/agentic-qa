"""G3 anomaly scan: offline sweep of recorded raw for anomaly classes -> pre-flight check
before G3-full (gap-research-plan.md). Zero cost, reuses telemetry already captured (§3.1),
same re-derivation principle as G4 -- no new runs, no sandbox.

Generalizes checks this session already ran by hand on individual runs (vlc_port,
chrome_inject_js, the 26455s/2193s sleep-inflated run), so a systemic issue surfaces
automatically before G3-full replicates it across 6.5x more runs:

  duration_outlier  -- agent_duration_ms with a modified z-score (median/MAD, Iglewicz &
                       Hoaglin) beyond the threshold -- robust to the very outlier it's meant
                       to catch, unlike quartile-based bounds, which a single extreme point can
                       drag along with it at the sample sizes G3 runs at
  cost_outlier      -- same rule on agent_cost_usd
  wall_clock_gap    -- provenance wall-clock time far exceeds agent_duration_ms -- the exact
                       signature of the 26455s/2193s split (host sleep, not an agent issue)
  turn_cap_hit      -- agent_num_turns >= config.MAX_TURNS
  dirty_finish      -- agent_clean_finish is False
  reason_cluster    -- the same underlying exception recurring across >=2 DIFFERENT tasks,
                       restricted to EVAL_ERROR/ENVIRONMENT_ERROR verdicts. Matched on the text
                       after "eval errored: " when present, not the whole reason -- the prefix
                       carries per-task eval_state content (found live 2026-08-09: three tasks
                       shared an identical JSONDecodeError but were missed by whole-string
                       matching because each had different captured eval_state text before it).
                       SUCCESS/FAILURE reasons repeat by design (every infeasible task shares
                       "official infeasible -> 0.00") and aren't a signal.

Scoped to g3_sample.full(), not just the k=40 stratified sample, so it keeps working
unmodified once G3-full lands on disk.

Run: python -m benchmarks.osworld.analysis.g3_anomaly_scan
"""
import json
from datetime import datetime
from statistics import median

from benchmarks.osworld import config
from benchmarks.osworld.analysis.g3_sample import full
from core.results import is_run_dir

MAD_Z_THRESHOLD = 3.5  # Iglewicz & Hoaglin's conventional cutoff for the modified z-score
WALL_CLOCK_GAP_S = 1800  # host-sleep signature: absolute gap this large isn't scheduling jitter


def _load_records(results_dir=None, system=None):
    """[(task_id, run_name, result, eval), ...] for every scored run of a task in
    g3_sample.full()."""
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    ids = set(full())
    out = []
    if not base.exists():
        return out
    for tid in ids:
        tdir = base / tid
        if not tdir.exists():
            continue
        for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and is_run_dir(p.name)):
            ev, res = rdir / "eval.json", rdir / "result.json"
            if not ev.exists() or not res.exists():
                continue
            try:
                out.append((tid, rdir.name, json.loads(res.read_text()), json.loads(ev.read_text())))
            except Exception:
                continue
    return out


def _mad_stats(values):
    if len(values) < 4:
        return None
    m = median(values)
    mad = median(abs(v - m) for v in values)
    return (m, mad) if mad else None


def _is_outlier(value, stats):
    if stats is None or value is None:
        return False
    m, mad = stats
    return abs(0.6745 * (value - m) / mad) > MAD_Z_THRESHOLD


def scan(results_dir=None, system=None):
    records = _load_records(results_dir, system)
    flags = {"duration_outlier": [], "cost_outlier": [], "wall_clock_gap": [],
              "turn_cap_hit": [], "dirty_finish": [], "reason_cluster": []}

    durations = [r["agent_duration_ms"] for _, _, r, _ in records if r.get("agent_duration_ms")]
    costs = [r["agent_cost_usd"] for _, _, r, _ in records if r.get("agent_cost_usd")]
    dur_stats, cost_stats = _mad_stats(durations), _mad_stats(costs)

    reason_tasks = {}  # reason -> set of task_ids, EVAL_ERROR/ENVIRONMENT_ERROR only

    for tid, rname, r, e in records:
        loc = f"{tid}/{rname}"
        dur, cost = r.get("agent_duration_ms"), r.get("agent_cost_usd")
        turns, clean = r.get("agent_num_turns"), r.get("agent_clean_finish")
        verdict, reason = e.get("verdict"), e.get("reason")
        prov = r.get("provenance") or {}

        if _is_outlier(dur, dur_stats):
            flags["duration_outlier"].append({"run": loc, "agent_duration_ms": dur})
        if _is_outlier(cost, cost_stats):
            flags["cost_outlier"].append({"run": loc, "agent_cost_usd": cost})
        if turns is not None and turns >= config.MAX_TURNS:
            flags["turn_cap_hit"].append({"run": loc, "agent_num_turns": turns})
        if clean is False:
            flags["dirty_finish"].append({"run": loc})

        started, finished = prov.get("started_at"), prov.get("finished_at")
        if started and finished and dur:
            try:
                wall_s = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
                agent_s = dur / 1000
                if wall_s - agent_s > WALL_CLOCK_GAP_S and wall_s > 3 * agent_s:
                    flags["wall_clock_gap"].append(
                        {"run": loc, "wall_clock_s": round(wall_s), "agent_duration_s": round(agent_s)})
            except ValueError:
                pass

        if reason and verdict in ("EVAL_ERROR", "ENVIRONMENT_ERROR"):
            key = reason.split("eval errored: ", 1)[-1]
            reason_tasks.setdefault(key, set()).add(tid)

    for reason, tids in reason_tasks.items():
        if len(tids) >= 2:
            flags["reason_cluster"].append({"reason": reason, "tasks": sorted(tids)})

    return flags


def _main():
    flags = scan(system=config.system_from_argv())
    total = sum(len(v) for v in flags.values())
    if not total:
        print("No anomalies flagged.")
        return
    print(f"G3 anomaly scan -- {total} flag(s)\n")
    for kind, items in flags.items():
        if not items:
            continue
        print(f"{kind} ({len(items)}):")
        for it in items[:10]:
            print(f"  {it}")
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more")
        print()


if __name__ == "__main__":
    _main()
