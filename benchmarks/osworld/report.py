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


def main():
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--compare":
        ab_compare(config.RESULTS_DIR, argv[1], argv[2], title="OSWorld")
        return
    system = argv[0] if argv else "agent_computer"
    summarize(config.RESULTS_DIR, system, title="OSWorld")
    _source_breakdown(system)
    _incidental_breakdown(system)


if __name__ == "__main__":
    main()
