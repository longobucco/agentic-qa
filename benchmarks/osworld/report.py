"""Print the OSWorld summary, plus the clean-vs-incidental-success breakdown that
core.reporting doesn't know about — it's an OSWorld-only concept: a SUCCESS the agent reached
without actually finishing cleanly (hit --max-turns / errored before printing ANSWER, and the
desktop state just happened to already satisfy the evaluator). See
runners/agent_computer.py::_annotate_incidental. `--compare A B` for an A/B.
"""
import json
import sys

from benchmarks.osworld import config
from core.reporting import ab_compare, summarize
from core.results import is_run_dir


def _eval_records(system):
    base = config.RESULTS_DIR / system
    if not base.exists():
        return
    for tdir in base.iterdir():
        if not tdir.is_dir():
            continue
        for rdir in tdir.iterdir():
            if not (rdir.is_dir() and is_run_dir(rdir.name)):
                continue
            ev = rdir / "eval.json"
            if not ev.exists():
                continue
            try:
                yield json.loads(ev.read_text())
            except Exception:
                continue


def _incidental_breakdown(system):
    total_success = incidental = 0
    for rec in _eval_records(system):
        if rec.get("verdict") == "SUCCESS":
            total_success += 1
            if "note" in rec:
                incidental += 1
    if total_success:
        print(f"\nOf {total_success} SUCCESS verdict(s), {incidental} incidental (agent didn't "
             f"finish cleanly — see eval.json 'note'), {total_success - incidental} clean.")


def official_mean_reward(system):
    """OSWorld's own aggregation: the mean of the evaluator's reward over scored runs, partial
    credit included (compare_images returns an SSIM such as 0.91, compare_docx_files 0.9996).
    That is the number comparable with published OSWorld scores; the binary SUCCESS rate
    (reward ~1.0 only) is reported next to it. Unscored runs (ENVIRONMENT_ERROR, EVAL_ERROR)
    are excluded, as in the pass rate."""
    rewards, successes = [], 0
    for rec in _eval_records(system):
        verdict = rec.get("verdict")
        if verdict not in ("SUCCESS", "FAILURE"):
            continue
        try:
            r = float(rec.get("reward"))
        except (TypeError, ValueError):
            r = 1.0 if verdict == "SUCCESS" else 0.0
        rewards.append(r)
        successes += verdict == "SUCCESS"
    if not rewards:
        return None
    return {"scored_runs": len(rewards), "mean_reward": sum(rewards) / len(rewards),
            "binary_success": successes / len(rewards)}


def _mean_reward_line(system):
    m = official_mean_reward(system)
    if m:
        print(f"OSWorld score (mean reward, partial credit): {100 * m['mean_reward']:.1f}% "
              f"vs binary success {100 * m['binary_success']:.1f}% over {m['scored_runs']} "
              f"scored run(s)")


def tool_surface_violation_count(system):
    """Runs the official protocol scored a terminal FAILURE because the agent used a tool
    other than `computer` (task 7b: eval.json carries `tool_surface_violation`, never
    infra_error.json). Reported on its own so it stays distinguishable from an ordinary agent
    FAILURE rather than silently blending into the pass rate."""
    return sum(1 for rec in _eval_records(system) if rec.get("tool_surface_violation"))


def _tool_surface_violation_line(system):
    n = tool_surface_violation_count(system)
    if n:
        print(f"Tool-surface violations (terminal FAILURE, agent used a non-`computer` "
             f"tool): {n}")


def _source_breakdown(system):
    """Who scored each run — OSWorld's own evaluator, or our weaker offline fallback (only
    covers exact_match/check_include_exclude — see evaluate.py)? Mixing the two into one pass
    rate without saying so hides which numbers are backed by the real evaluator."""
    counts = {}
    for rec in _eval_records(system):
        src = rec.get("source", "(unknown)")
        counts[src] = counts.get(src, 0) + 1
    scored = {k: v for k, v in counts.items() if k != "environment"}
    if scored:
        total = sum(scored.values())
        parts = ", ".join(f"{v} {k}" for k, v in sorted(scored.items(), key=lambda kv: -kv[1]))
        print(f"Scored by: {parts} (of {total})")
        if counts.get("offline_fallback"):
            print(f"  ⚠ {counts['offline_fallback']} run(s) scored by the offline fallback, not "
                  f"OSWorld's own evaluator — treat those verdicts as less trustworthy.")


def _infra_last_records(system):
    """Last infra_error.json entry per run dir (write_infra_error appends; only the latest
    attempt's outcome matters for a snapshot report)."""
    base = config.RESULTS_DIR / system
    if not base.exists():
        return
    for tdir in base.iterdir():
        if not tdir.is_dir():
            continue
        for rdir in tdir.iterdir():
            if not (rdir.is_dir() and is_run_dir(rdir.name)):
                continue
            path = rdir / "infra_error.json"
            if not path.exists():
                continue
            try:
                records = json.loads(path.read_text())
                if records:
                    yield records[-1]
            except Exception:
                continue


def _openbook_breakdown(system):
    """Open-book-only breakdown (docs/g_astra_open_book_runner_implementation.md's "Reporting"):
    scored task/run count by app/reason/snapshot profile, inconclusive counts by infra class,
    rate-limit attempts excluded from performance metrics, and the digests every aggregate was
    computed under -- deliberately never averaged together with the closed-book report's own
    numbers (a different system name, a separate `python -m benchmarks.osworld.report` call)."""
    manifest_path = config.HERE / "astra_openbook_manifest.json"
    tags_by_id = {}
    try:
        manifest = json.loads(manifest_path.read_text())
        tags_by_id = {t["task_id"]: t for t in manifest["tasks"]}
    except Exception:
        pass

    scored, by_app, by_reason, by_profile = 0, {}, {}, {}
    for rec in _eval_records(system):
        if rec.get("source") == "environment":
            continue
        scored += 1
        info = tags_by_id.get(rec.get("id"), {})
        by_app[info.get("app", "?")] = by_app.get(info.get("app", "?"), 0) + 1
        for reason in info.get("reasons") or ["?"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        profile = info.get("snapshot_profile", "?")
        by_profile[profile] = by_profile.get(profile, 0) + 1

    inconclusive, rate_limited = {}, 0
    for rec in _infra_last_records(system):
        outcome = rec.get("outcome", "?")
        if outcome == "RATE_LIMITED":
            rate_limited += 1
        else:
            inconclusive[outcome] = inconclusive.get(outcome, 0) + 1

    lock_path = config.HERE / "astra_openbook_campaign_lock.json"
    lock_status = None
    try:
        lock_status = json.loads(lock_path.read_text()).get("status")
    except Exception:
        pass

    print(f"\nOpen-book: {scored} scored run(s) (excludes ENVIRONMENT_ERROR); "
         f"{rate_limited} rate-limited attempt(s) excluded from performance metrics; "
         f"campaign lock status={lock_status!r}.")
    if by_app:
        print("  by app:      " + ", ".join(f"{k}={v}" for k, v in sorted(by_app.items())))
    if by_reason:
        print("  by reason:   " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    if by_profile:
        print("  by profile:  " + ", ".join(f"{k}={v}" for k, v in sorted(by_profile.items())))
    if inconclusive:
        print("  inconclusive by class: "
             + ", ".join(f"{k}={v}" for k, v in sorted(inconclusive.items())))


def main():
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--compare":
        ab_compare(config.RESULTS_DIR, argv[1], argv[2], title="OSWorld")
        return
    system = argv[0] if argv else "agent_computer"
    summarize(config.RESULTS_DIR, system, title="OSWorld")
    _source_breakdown(system)
    _mean_reward_line(system)
    _incidental_breakdown(system)
    _tool_surface_violation_line(system)
    if system == config.ASTRA_OPENBOOK_SYSTEM_NAME:
        _openbook_breakdown(system)


if __name__ == "__main__":
    main()
