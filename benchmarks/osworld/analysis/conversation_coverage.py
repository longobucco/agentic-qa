"""Conversation-transcript coverage manifest -- which tasks have a real conversation.jsonl saved,
re-derived from what's on disk, no new runs.

Conversation capture (agent_computer.py::_save_conversation_transcript) started 2026-08-24, well
after the campaign itself, so coverage is partial by construction and grows unevenly: a
2026-08-25/26 backfill batch specifically targeted app/verdict diversity among the previously
uncovered tasks (see g_convo_batch_ids.txt in scripts/), so "how many tasks have a transcript" is
not a fixed number quoted once -- it changes as more backfill batches run, hence a script here
rather than a hand-maintained list.

A run's conversation.jsonl is not automatically a *useful* transcript: a rate-limited attempt
(agent_api_error_status=429, 1 turn, no real agent work -- see agent_computer.py's
_rate_limit_infra_rec) still gets a 9-line stub written by the same capture path, since the CLI
still returns a session id to copy from. Same distinction as g8_failure_taxonomy's INFRA layer:
a stub is on disk but tells you nothing about agent behavior. "real" below means
agent_api_error_status is unset for that run.

Run: python -m benchmarks.osworld.analysis.conversation_coverage [--json]
"""
import json
import sys

from benchmarks.osworld import config
from benchmarks.osworld import tasks as osw_tasks
from benchmarks.osworld.tasks import load_tasks
from core.results import is_run_dir


def coverage(results_dir=None, system=None):
    """{task_id: {"app": ..., "real": [run_idx, ...], "stub": [run_idx, ...]}} for every task
    that has at least one conversation.jsonl on disk, real or stub.

    Iterates task dirs actually on disk (like g8_failure_taxonomy's load_cohort), not the
    runnable-task population -- a directory outside the current scope still deserves an entry
    if it has a transcript, and it's what makes this testable against a synthetic tempdir."""
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    base = results_dir / system
    tasks = {t["id"]: t for t in load_tasks()}
    out = {}
    if not base.exists():
        return out

    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        tid = tdir.name
        real, stub = [], []
        for rdir in sorted(p for p in tdir.iterdir() if p.is_dir() and is_run_dir(p.name)):
            if not (rdir / "conversation.jsonl").exists():
                continue
            n = int(rdir.name.split("_")[1])
            try:
                result = json.loads((rdir / "result.json").read_text())
            except Exception:
                result = {}
            (stub if result.get("agent_api_error_status") else real).append(n)
        if real or stub:
            out[tid] = {"app": osw_tasks.app_of(tasks.get(tid, {})),
                        "real": real, "stub": stub}
    return out


def _main():
    cov = coverage(system=config.system_from_argv())
    if "--json" in sys.argv:
        print(json.dumps(cov, indent=2))
        return

    n_tasks = len(cov)
    n_real_tasks = sum(1 for d in cov.values() if d["real"])
    n_real_runs = sum(len(d["real"]) for d in cov.values())
    n_stub_runs = sum(len(d["stub"]) for d in cov.values())
    n_population = len(load_tasks())

    print(f"CONVERSATION COVERAGE  {n_real_tasks}/{n_population} tasks have >=1 real transcript "
          f"({n_tasks} tasks have >=1 conversation.jsonl on disk, real or stub)")
    print(f"  real transcripts: {n_real_runs} runs   stub (rate-limited) transcripts: {n_stub_runs} runs\n")

    print(f"{'Task':10s} {'App':20s} {'Real runs':10s} {'Stub runs':10s}")
    for tid, d in sorted(cov.items(), key=lambda kv: (kv[1]["app"], kv[0])):
        if not d["real"]:
            continue
        print(f"{tid[:8]:10s} {d['app']:20s} {str(d['real']):10s} {str(d['stub']) if d['stub'] else '':10s}")


if __name__ == "__main__":
    _main()
