#!/usr/bin/env python3
"""Extract the failed tasks from a WebVoyager-style results directory.

Handles both layouts: our own output (one trajectory per `run_<k>/eval.json`, possibly
several per task) and Alumnium's fork output (a flat `<task>/eval.json`). This is the
deliverable: the ~1.5% of tasks Alumnium failed — published nowhere — which you regenerate
by running the pipeline, then run this on the results dir.

Usage:
  python -m benchmarks.webvoyager.extract_failures <results_dir>
  python -m benchmarks.webvoyager.extract_failures benchmarks/webvoyager/results/alumnium
  python -m benchmarks.webvoyager.extract_failures \
      benchmarks/webvoyager/alumnium/vendor/alumnium/benchmarks/webvoyager/results/claude-code
"""
import json
import sys
from pathlib import Path


def _verdict_of_eval(ev: Path):
    """Return ('SUCCESS'|'FAILURE', reason) for one eval.json, tolerant of formats."""
    try:
        data = json.loads(ev.read_text())
    except Exception as e:
        return "FAILURE", f"unparseable eval.json: {e}"
    blob = json.dumps(data).upper()
    if isinstance(data, dict):
        reason = str(data.get("reason") or data.get("response") or "")[:300]
        v = str(data.get("verdict", "")).upper()
        if v:
            ok = v.startswith("SUCC") and "NOT" not in v
            return ("SUCCESS" if ok else "FAILURE"), reason
    else:
        reason = ""
    tail = blob[blob.rfind("VERDICT"):] if "VERDICT" in blob else blob
    ok = "SUCCESS" in tail and "NOT SUCCESS" not in tail and "FAILURE" not in tail
    return ("SUCCESS" if ok else "FAILURE"), reason


def verdict_of(task_dir: Path):
    """Aggregate a task's verdict across its run_*/eval.json (or a flat eval.json).

    Majority vote over runs; ties break to FAILURE. Returns (verdict, reason) or (None, _).
    """
    evals = sorted(task_dir.glob("run_*/eval.json"))
    if not evals and (task_dir / "eval.json").exists():
        evals = [task_dir / "eval.json"]
    if not evals:
        return None, "no eval.json"
    results = [_verdict_of_eval(ev) for ev in evals]
    succ = [r for r in results if r[0] == "SUCCESS"]
    if len(succ) * 2 > len(results):
        return "SUCCESS", succ[0][1]
    fails = [r for r in results if r[0] != "SUCCESS"]
    suffix = f" [{len(succ)}/{len(results)} runs ok]" if len(results) > 1 else ""
    return "FAILURE", (fails[0][1] if fails else "") + suffix


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
