"""Per-task manifest of everything executed so far: one row per in-scope task.

Why this exists: `report.py` prints a pooled summary and never persists it, and the pass rate
it prints is a MIXTURE -- for the 289 tasks the transcript backfill re-ran, run_N/ now holds
the backfill's rollouts; for the rest it still holds the original G3 campaign's. Any document
that quotes one number without saying which condition produced each row is misleading, so
every row here declares its own condition and carries the preserved G3 baseline verdict beside
the current one.

    python -m benchmarks.osworld.analysis.task_manifest
      -> analysis/results/task_manifest.csv   (openable in Excel/Numbers)
      -> analysis/results/task_manifest.json  (same rows, for further analysis)
"""
import collections
import csv
import json
import pathlib

from benchmarks.osworld import config, tasks as osw_tasks

RESULTS = config.RESULTS_DIR / "agent_computer"
OUT_DIR = pathlib.Path("benchmarks/osworld/analysis/results")

# Campaign phase boundaries, by run start time. The G5 ablation arms and then the transcript
# backfill both used --force, so a task's current run_N/ may hold any of three conditions --
# this is what separates them (see docs/g5-arm-*.md and scripts/g_convo_backfill2_*).
G5_ARMS_FROM = "2026-08-28"
BACKFILL_FROM = "2026-09-04T09:02:20"

VERDICT_SHORT = {"SUCCESS": "S", "FAILURE": "F",
                 "EVAL_ERROR": "E", "ENVIRONMENT_ERROR": "X"}


def _read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _has_real_transcript(path):
    """A rate-limited run still writes a 1-turn stub transcript; only a tool_use block proves
    the agent actually acted. This is the distinction `transcript_saved` cannot make."""
    if not path.exists():
        return False
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        ):
            return True
    return False


def _condition(started_ats):
    """Which campaign phase produced the runs currently sitting in run_N/."""
    dates = [s for s in started_ats if s]
    if not dates:
        return "no-run"
    labels = set()
    for s in dates:
        if s >= BACKFILL_FROM:
            labels.add("backfill")
        elif s >= G5_ARMS_FROM:
            labels.add("g5-arm")
        else:
            labels.add("g3-original")
    return sorted(labels)[0] if len(labels) == 1 else "mixed:" + "+".join(sorted(labels))


def _collect(task):
    tid = task["id"]
    td = RESULTS / tid
    row = {
        "task_id": tid,
        "app": osw_tasks.bucket_of(task),
        "evaluator": json.dumps(task.get("evaluator", {}).get("func")),
        "instruction": " ".join((task.get("instruction") or "").split())[:110],
    }
    if not td.is_dir():
        row.update({"condition": "never-run", "current_verdicts": "", "baseline_verdicts": "",
                    "reward": "", "scored_by": "", "real_transcripts": 0,
                    "runs_on_disk": 0, "note": "no results directory"})
        return row

    cur, base, starts, rewards, sources, n_real = [], [], [], [], [], 0
    for k in (1, 2, 3):
        rd, legacy = td / f"run_{k}", td / f"run_{k}_g3baseline_legacy"
        ev, res = _read_json(rd / "eval.json"), _read_json(rd / "result.json")
        if ev:
            cur.append(VERDICT_SHORT.get(ev.get("verdict"), "?"))
            if ev.get("reward") is not None:
                rewards.append(ev["reward"])
            if ev.get("source"):
                sources.append(ev["source"])
        if res:
            starts.append((res.get("provenance") or {}).get("started_at", ""))
        lev = _read_json(legacy / "eval.json")
        if lev:
            base.append(VERDICT_SHORT.get(lev.get("verdict"), "?"))
        if _has_real_transcript(rd / "conversation.jsonl"):
            n_real += 1

    row.update({
        "condition": _condition(starts),
        "current_verdicts": "".join(cur),
        "baseline_verdicts": "".join(base),
        "reward": round(sum(rewards) / len(rewards), 2) if rewards else "",
        "scored_by": "/".join(sorted(set(sources))) if sources else "",
        "real_transcripts": n_real,
        "runs_on_disk": len(cur),
        "note": "",
    })
    # Only a verdict flip between two runs of the SAME condition is agent variance; a flip
    # against the preserved baseline may instead be replication drift, so mark it distinctly.
    if row["baseline_verdicts"] and row["current_verdicts"] != row["baseline_verdicts"]:
        if row["condition"] == "backfill":
            row["note"] = "differs from G3 baseline"
    return row


def main():
    all_tasks = osw_tasks.load_tasks()
    rows = [_collect(t) for t in all_tasks]
    rows.sort(key=lambda r: (r["app"], r["task_id"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["task_id", "app", "condition", "current_verdicts", "baseline_verdicts",
              "reward", "scored_by", "real_transcripts", "runs_on_disk", "evaluator",
              "note", "instruction"]
    with (OUT_DIR / "task_manifest.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (OUT_DIR / "task_manifest.json").write_text(json.dumps(rows, indent=2))

    by_cond = collections.Counter(r["condition"] for r in rows)
    with_transcripts = sum(1 for r in rows if r["real_transcripts"] == 3)
    partial = sum(1 for r in rows if 0 < r["real_transcripts"] < 3)
    drifted = sum(1 for r in rows if r["note"] == "differs from G3 baseline")

    print(f"task in scope: {len(rows)}  ->  {OUT_DIR}/task_manifest.{{csv,json}}")
    print("\ncondizione delle run attualmente su disco:")
    for cond, n in by_cond.most_common():
        print(f"  {cond:<28} {n}")
    print(f"\ntrascrizioni reali: {with_transcripts} task complete (3/3), "
          f"{partial} parziali, {len(rows) - with_transcripts - partial} assenti")
    print(f"verdetto diverso dalla baseline G3 (solo condizione backfill): {drifted}")


if __name__ == "__main__":
    main()
