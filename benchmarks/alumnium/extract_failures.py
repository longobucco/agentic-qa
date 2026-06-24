#!/usr/bin/env python3
"""Extract the failed tasks from a WebVoyager-style results directory.

Works on Alumnium's fork output (`benchmarks/webvoyager/results/claude-code/<task>/eval.json`)
and on our own harness output. This is the deliverable: the ~1.5% of tasks that failed,
which is published nowhere — you regenerate it by running the pipeline, then run this.

Usage:
  python extract_failures.py <results_dir>
  python extract_failures.py ../webvoyager/results/alumnium
  python extract_failures.py ./vendor/alumnium/benchmarks/webvoyager/results/claude-code
"""
import json
import sys
from pathlib import Path


def verdict_of(task_dir: Path):
    """Return ('SUCCESS'|'FAILURE', reason) from a task dir, tolerant of formats."""
    ev = task_dir / "eval.json"
    if not ev.exists():
        return None, "no eval.json"
    try:
        data = json.loads(ev.read_text())
    except Exception as e:
        return "FAILURE", f"unparseable eval.json: {e}"

    blob = json.dumps(data).upper()
    reason = ""
    if isinstance(data, dict):
        reason = str(data.get("reason") or data.get("response") or "")[:300]
        v = str(data.get("verdict", "")).upper()
        if v:
            ok = v.startswith("SUCC") and "NOT" not in v
            return ("SUCCESS" if ok else "FAILURE"), reason
    tail = blob[blob.rfind("VERDICT"):] if "VERDICT" in blob else blob
    ok = "SUCCESS" in tail and "NOT SUCCESS" not in tail and "FAILURE" not in tail
    return ("SUCCESS" if ok else "FAILURE"), reason


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    base = Path(sys.argv[1]).expanduser().resolve()
    if not base.exists():
        sys.exit(f"Not found: {base}")

    rows = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        verdict, reason = verdict_of(d)
        if verdict is None:
            continue
        rows.append({"id": d.name, "site": d.name.split("--")[0],
                     "verdict": verdict, "reason": reason})

    if not rows:
        sys.exit(f"No eval.json files under {base}")

    fails = [r for r in rows if r["verdict"] != "SUCCESS"]
    n, ok = len(rows), sum(1 for r in rows if r["verdict"] == "SUCCESS")
    print(f"Evaluated {n} tasks | {ok} success | {len(fails)} failed | "
          f"{100*ok/n:.2f}% success rate\n")
    print(f"FAILED TASKS ({len(fails)}):")
    for r in fails:
        print(f"  - {r['id']:<22} {r['reason']}")

    out = base.parent / "failed_tasks.json"
    out.write_text(json.dumps(fails, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
