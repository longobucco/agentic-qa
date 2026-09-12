"""Build the frozen 30-task Verify-Replan pilot manifest (docs/verify-replan-minimal-integration-
plan.md Section 13): 10 known false-completion tasks, 10 flaky/near-boundary tasks, 10
always-pass/always-fail controls (5 + 5) -- everything derived from data already on disk in
results/agent_computer_sonnet5/, zero new sandbox/API spend to build this. Chrome and the 9
Google-Drive-credential-blocked tasks are excluded, matching every other pinned campaign's scope
in this project.

Writes benchmarks/osworld/verify_replan_pilot_manifest.json: task ids per category, a combined
sha256 (same tamper-evidence pattern as astra_campaign_lock.json), and the selection criteria
recorded so the manifest is self-documenting. Deterministic: sorted task ids, first N per
category after exclusions -- rerunning this script against the SAME results tree reproduces the
identical manifest.

Run: python -m scripts.build_verify_replan_pilot_manifest
"""
import hashlib
import json
from pathlib import Path

from benchmarks.osworld import config
from benchmarks.osworld.analysis.g8_failure_taxonomy import taxonomy
from benchmarks.osworld.tasks import bucket_of, load_tasks

N_PER_CATEGORY = 10
BASE = config.RESULTS_DIR / "agent_computer_sonnet5"
EXCLUDED_IDS_FILE = Path(__file__).parent / "g_astra_campaign_google_drive_excluded_ids.txt"


def _excluded_ids():
    text = EXCLUDED_IDS_FILE.read_text() if EXCLUDED_IDS_FILE.exists() else ""
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def _false_completion_ids(tasks):
    """A run where the agent claimed DONE but the official verdict was FAILURE -- the exact
    failure mode Verify-Replan targets (Section 1)."""
    ids = []
    for tdir in sorted(BASE.iterdir()):
        if not tdir.is_dir() or tdir.name not in tasks:
            continue
        for n in (1, 2, 3):
            r, e = tdir / f"run_{n}" / "result.json", tdir / f"run_{n}" / "eval.json"
            if not (r.exists() and e.exists()):
                continue
            try:
                res, ev = json.loads(r.read_text()), json.loads(e.read_text())
            except Exception:
                continue
            if (res.get("answer") or "").strip().upper() == "DONE" and ev.get("verdict") == "FAILURE":
                ids.append(tdir.name)
                break
    return ids


def build():
    tasks = {t["id"]: t for t in load_tasks()}
    excluded = _excluded_ids()

    def in_scope(tid):
        return (tid in tasks and tid not in excluded and bucket_of(tasks[tid]) != "chrome")

    t = taxonomy(config.RESULTS_DIR, "agent_computer_sonnet5")
    false_completion = sorted(i for i in _false_completion_ids(tasks) if in_scope(i))[:N_PER_CATEGORY]
    flaky = sorted(i for i in t["stability"]["flaky"]
                   if in_scope(i) and i not in false_completion)[:N_PER_CATEGORY]
    half = N_PER_CATEGORY // 2
    used = set(false_completion) | set(flaky)
    always_pass = sorted(i for i in t["stability"]["always_pass"]
                         if in_scope(i) and i not in used)[:half]
    always_fail = sorted(i for i in t["stability"]["always_fail"]
                         if in_scope(i) and i not in used)[:N_PER_CATEGORY - half]
    controls = sorted(always_pass + always_fail)

    all_ids = sorted(set(false_completion) | set(flaky) | set(controls))
    if len(all_ids) != len(false_completion) + len(flaky) + len(controls):
        raise SystemExit("duplicate task id across pilot categories -- fix the selection before freezing")

    manifest = {
        "plan": "docs/verify-replan-minimal-integration-plan.md",
        "built_from": "agent_computer_sonnet5",
        "criteria": {
            "false_completion": "answer=DONE, official verdict=FAILURE on >=1 existing run",
            "flaky": "g8_failure_taxonomy stability=flaky, not already in false_completion",
            "controls": f"{half} always_pass + {N_PER_CATEGORY - half} always_fail, "
                        f"not already selected above",
        },
        "excluded": {"chrome_bucket": True, "google_drive_ids_file": str(EXCLUDED_IDS_FILE)},
        "categories": {
            "false_completion": false_completion,
            "flaky": flaky,
            "controls": controls,
        },
        "all_ids": all_ids,
        "count": len(all_ids),
        "sha256": hashlib.sha256(("\n".join(all_ids) + "\n").encode()).hexdigest(),
    }
    out_path = Path(config.HERE) / "verify_replan_pilot_manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"wrote {out_path} -- {len(all_ids)} task ids "
          f"({len(false_completion)} false_completion, {len(flaky)} flaky, {len(controls)} controls)")
    print(f"sha256: {manifest['sha256']}")


if __name__ == "__main__":
    build()
