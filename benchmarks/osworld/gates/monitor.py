"""Gate B: the validity monitor, run after every run. Zero quota.

    OSW_PHASE3_SUFFIX=pkgfix3c .venv/bin/python -m benchmarks.osworld.gates.monitor [--halt]

Checks every judged run of the campaign's trees against the invariants the lock declares, plus rate
alarms on a sliding window and the pairing/balance checks. With --halt a violation writes
docs/phase3/STOP and docs/phase3/HALT.json and the driver stops: at 144 runs a day a
misconfiguration found two days late has already produced ~290 runs to throw away.

A halt is never lifted automatically. It means something has to be diagnosed.
"""
import argparse
import json
import sys
from pathlib import Path

from benchmarks.osworld import config

ROOT = Path(__file__).resolve().parents[3]
LOCK = Path(__file__).resolve().parent.parent / "x22_campaign_lock.json"
PHASE3_DIR = ROOT / "docs" / "phase3"
WINDOW = 40          # runs of a tree the rate alarms look back over
RATE_ALARMS = {      # share of the window that triggers a halt
    "tool_surface_violation": 0.05,   # `code` sat at 7.1%; code2's contract exists to fix that
    "environment_error": 0.10,
    "eval_error": 0.05,
    "no_verdict": 0.50,
}
# G17 (X6): a null agent passes these. A SUCCESS shows no capability and a FAIL destroys an
# already-passing initial state, so their presence outside a declared set is worth flagging.
G17 = {"b6781586", "28cc3b7e", "99146c54", "3299584d", "4d117223", "93eabf48", "9219480b"}


def _expected(lock, arm):
    """The provenance a run of `arm` must carry."""
    exp = {
        "model_served": lock["model"], "model_mismatch": False,
        "reasoning_effort_served": lock["reasoning_effort"], "reasoning_effort_mismatch": False,
        "cli_version_served": lock["codex_cli_version"],
        "approval_policy_served": "never", "sandbox_policy_served": "danger-full-access",
        "permission_profile_served": "disabled",
        "image": lock["image"], "evaluator_commit": lock["evaluator_commit"],
        "setup_controller_commit": lock["evaluator_commit"], "evaluator_source": "pinned",
        "max_steps": lock["max_steps"], "screen_size": lock["screen_size"],
        "observation": "screenshot", "action_space": "computer_20251124",
        "protocol": "official", "release": "verified", "backend": "daytona",
        "clock_exempt": False, "harness_dirty": False,
        "arm": None if arm == "a0" else arm,
        "arm_addendum_sha256": None if arm == "a0" else lock["arm_addendum_sha256"][arm],
    }
    return exp


def check_run(path, lock, arm):
    """Reasons this single run is not valid ([] = clear). `path` is the run directory."""
    bad = []
    try:
        result = json.loads((path / "result.json").read_text())
    except (OSError, ValueError) as e:
        return [f"result.json unreadable: {type(e).__name__}"]
    prov = result.get("provenance") or {}
    for field, want in _expected(lock, arm).items():
        got = prov.get(field)
        if got != want:
            bad.append(f"provenance.{field} = {got!r}, expected {want!r}")
    # measured, not asserted
    if not result.get("transcript_saved") or not (result.get("transcript_bytes") or 0) > 0:
        bad.append("transcript not saved")
    if result.get("rollout_archived") is not True:
        bad.append(f"Codex rollout not archived ({result.get('rollout_error')!r}): the trajectory "
                   "would exist only under CODEX_HOME")
    # [] means verified clean; None means unverifiable, which must never look clean
    for field in ("agent_context_leaks", "agent_non_computer_tool_calls"):
        if result.get(field) != []:
            bad.append(f"{field} = {result.get(field)!r} (expected [])")
    if result.get("contaminated"):
        bad.append("tool audit flagged possible oracle access")
    if result.get("agent_steps_used") is None:
        bad.append("agent_steps_used is null: the MCP server never reported its state")
    if not (path / "final_state" / "manifest.json").exists():
        bad.append("V1 final-state capture missing")
    return bad


def scan(lock, results_dir=None):
    """Per-run invariants and per-tree rate alarms over the campaign's trees."""
    results_dir = Path(results_dir) if results_dir else config.RESULTS_DIR
    trees = {g["tree"]: g["arm"] for g in lock["groups"]}
    report = {"invariant_violations": [], "rate_alarms": [], "flags": [],
              "trees": {}, "runs_checked": 0}
    for tree, arm in trees.items():
        root = results_dir / tree
        if not root.exists():
            continue
        runs = sorted((p for p in root.glob("*/run_*") if p.is_dir()),
                      key=lambda p: p.stat().st_mtime)
        stats = {"judged": 0, "tool_surface_violation": 0, "environment_error": 0,
                 "eval_error": 0, "no_verdict": 0, "infra": 0}
        window = runs[-WINDOW:]
        for p in window:
            # core.results: infra_error.json is an APPEND-ONLY LOG of attempts that died before
            # producing a verdict, and is_done() looks at eval.json alone. A run carrying both
            # flaked, was retried, and then produced a verdict -- it must be counted. Excluding
            # any run that has the file would have silently dropped 339 judged runs of the
            # exploratory Astra trees (217 SUCCESS, 122 FAILURE) and under-checked the campaign.
            ev = p / "eval.json"
            if not ev.exists():
                if (p / "infra_error.json").exists():
                    stats["infra"] += 1      # not scored, resume retries it
                else:
                    stats["no_verdict"] += 1
                continue
            try:
                e = json.loads(ev.read_text())
            except ValueError:
                stats["no_verdict"] += 1
                continue
            verdict = e.get("verdict")
            if verdict == "ENVIRONMENT_ERROR":
                stats["environment_error"] += 1
                continue
            if verdict == "EVAL_ERROR":
                stats["eval_error"] += 1
                continue
            if verdict not in ("SUCCESS", "FAILURE"):
                stats["no_verdict"] += 1
                continue
            stats["judged"] += 1
            if e.get("tool_surface_violation"):
                stats["tool_surface_violation"] += 1
            report["runs_checked"] += 1
            for reason in check_run(p, lock, arm):
                report["invariant_violations"].append(
                    {"run": str(p.relative_to(results_dir)), "reason": reason})
            task = p.parent.name[:8]
            if task in G17:
                report["flags"].append({"run": str(p.relative_to(results_dir)),
                                        "flag": "G17 null-agent-pass task"})
        denom = max(1, len(window) - stats["infra"])
        for key, threshold in RATE_ALARMS.items():
            if stats[key] / denom > threshold:
                report["rate_alarms"].append(
                    {"tree": tree, "alarm": key, "rate": round(stats[key] / denom, 3),
                     "threshold": threshold, "window": denom})
        report["trees"][tree] = stats
    return report


def balance(lock, results_dir=None):
    """Pairing checks: the arms of a stage must stay level, task by task."""
    results_dir = Path(results_dir) if results_dir else config.RESULTS_DIR
    out = []
    by_stage = {}
    for g in lock["groups"]:
        by_stage.setdefault((g["stage"], g["set"]), []).append(g)
    for (stage, set_name), groups in sorted(by_stage.items()):
        counts = {}
        for g in groups:
            root = results_dir / g["tree"]
            for task_dir in (root.glob("*") if root.exists() else []):
                if not task_dir.is_dir():
                    continue
                n = len([p for p in task_dir.glob("run_*") if (p / "eval.json").exists()])
                counts.setdefault(task_dir.name, {})[g["arm"]] = n
        for task, per_arm in counts.items():
            if len(per_arm) < len(groups):
                continue   # not yet started on every arm: not an imbalance
            lo, hi = min(per_arm.values()), max(per_arm.values())
            if hi - lo > 1:
                out.append({"stage": stage, "set": set_name, "task": task[:8],
                            "per_arm": per_arm,
                            "reason": "arms differ by more than one judged run"})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m benchmarks.osworld.gates.monitor")
    ap.add_argument("--halt", action="store_true", help="write STOP and HALT.json on a violation")
    ap.add_argument("--results-dir", default=None)
    a = ap.parse_args(argv)
    lock = json.loads(LOCK.read_text())
    report = scan(lock, a.results_dir)
    report["balance"] = balance(lock, a.results_dir)

    print(f"gate B: {report['runs_checked']} judged run(s) checked across "
          f"{len(report['trees'])} tree(s)")
    for tree, s in report["trees"].items():
        print(f"  {tree}: {s}")
    fatal = report["invariant_violations"] + report["rate_alarms"] + report["balance"]
    for item in report["flags"]:
        print(f"  FLAG {item}")
    if not fatal:
        print("gate B ok")
        return 0
    print(f"\ngate B HALT: {len(report['invariant_violations'])} invariant violation(s), "
          f"{len(report['rate_alarms'])} rate alarm(s), {len(report['balance'])} balance "
          f"problem(s)", file=sys.stderr)
    for item in fatal[:25]:
        print(f"  - {item}", file=sys.stderr)
    if a.halt:
        PHASE3_DIR.mkdir(parents=True, exist_ok=True)
        (PHASE3_DIR / "STOP").touch()
        (PHASE3_DIR / "HALT.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nSTOP written; reasons in {PHASE3_DIR / 'HALT.json'}. A halt is not lifted "
              "automatically: diagnose, then phase3ctl.sh start.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
