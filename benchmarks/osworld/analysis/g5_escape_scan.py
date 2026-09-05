"""Full-transcript sandbox-escape scan (docs/finding-full-transcript-escape-scan.md). Idea #10
re-tested exactly the 4 tasks a manual transcript sample had already flagged -- a biased
selection, not a systematic scan. This scans every ORIGINAL baseline transcript available on
disk (the run_N_g3baseline_legacy/ copy where a later G5 arm overwrote run_N/, current run_N/
otherwise) for tool_use blocks outside the mcp__osworld__ namespace.

ToolSearch (the CLI's deferred-tool-schema lookup) is excluded -- it appears in nearly every
transcript as the agent probes whether a name resolves, and is not itself an escape; only a
real tool_use from a resolved built-in/foreign-MCP tool counts.

Run: python -m benchmarks.osworld.analysis.g5_escape_scan [--json]
"""
import collections
import json
import sys

from benchmarks.osworld import config
from benchmarks.osworld.tasks import load_tasks
from core.results import is_run_dir

NOT_ESCAPE = {"ToolSearch"}


def _is_real(run_dir):
    try:
        res = json.loads((run_dir / "result.json").read_text())
    except Exception:
        return False
    return not res.get("agent_api_error_status")


def _tool_calls(conversation_path):
    counts = collections.Counter()
    try:
        lines = conversation_path.read_text().splitlines()
    except Exception:
        return counts
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or ""
                if not name.startswith("mcp__osworld__") and name not in NOT_ESCAPE:
                    counts[name] += 1
    return counts


def scan(results_dir=None, system="agent_computer"):
    """{task_id: Counter(tool_name -> calls)} for every task with a genuine escape in its
    original-baseline transcript. Prefers run_N_g3baseline_legacy/ (the true G3 baseline,
    preserved before a later G5 arm overwrote run_N/) over current run_N/."""
    results_dir = results_dir or config.RESULTS_DIR
    base = results_dir / system
    escaped = collections.defaultdict(collections.Counter)
    n_scanned = 0
    if not base.exists():
        return escaped, n_scanned

    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        for rdir in sorted(p for p in tdir.iterdir() if p.is_dir()):
            name = rdir.name
            if name.endswith("_g3baseline_legacy"):
                candidate = rdir
            elif is_run_dir(name):
                if (tdir / f"{name}_g3baseline_legacy").exists():
                    continue   # the legacy copy is the real baseline; don't double-count
                candidate = rdir
            else:
                continue
            conv = candidate / "conversation.jsonl"
            if not conv.exists() or not _is_real(candidate):
                continue
            n_scanned += 1
            counts = _tool_calls(conv)
            if counts:
                escaped[tdir.name] += counts
    return escaped, n_scanned


def _main():
    escaped, n_scanned = scan()
    tasks = {t["id"]: t for t in load_tasks()}
    if "--json" in sys.argv:
        print(json.dumps({tid: dict(c) for tid, c in escaped.items()}, indent=2))
        return
    print(f"{n_scanned} original-baseline transcripts scanned, {len(escaped)} tasks escaped\n")
    for tid, counts in sorted(escaped.items(), key=lambda kv: -sum(kv[1].values())):
        app = (tasks.get(tid, {}).get("related_apps") or ["?"])[0]
        print(f"{tid[:8]} {app:20s} {dict(counts)}")


if __name__ == "__main__":
    _main()
