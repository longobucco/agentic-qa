"""Generate the open-book retry queue from result artifacts -- never hand-edited (spec's own
requirement: "The retry driver accepts a generated JSON queue, not a hand-edited implicit
list."). Reads benchmarks/osworld/results/agent_computer_astra_openbook/<task_id>/run_*/ and
classifies each run without an official verdict into one queue, with the error class, attempt
count, next eligible timestamp, and the campaign lock digest the queue was built for -- the retry
driver refuses a queue built for a different lock.

    python -m scripts.build_astra_open_book_retry_queue > scripts/g_astra_open_book_retry_queue.json
"""
import json
import sys
import time

from benchmarks.osworld import config, open_book_preflight as p

_BACKOFF_S = 300   # flat backoff per attempt; the driver's own usage-limit heuristic adds more


def _run_dirs(system):
    base = config.RESULTS_DIR / system
    if not base.exists():
        return
    for tdir in sorted(base.iterdir()):
        if not tdir.is_dir():
            continue
        for rdir in sorted(tdir.iterdir()):
            if rdir.is_dir() and rdir.name.startswith("run_"):
                yield tdir.name, rdir


def _classify(task_id, run_dir):
    if (run_dir / "eval.json").exists():
        return None   # completed AND scored -- immutable, never queued
    infra_path = run_dir / "infra_error.json"
    attempts = 0
    outcome = None
    if infra_path.exists():
        try:
            records = json.loads(infra_path.read_text())
            attempts = len(records)
            outcome = records[-1].get("outcome") if records else None
        except (json.JSONDecodeError, OSError):
            outcome = "UNREADABLE_INFRA_ERROR"
    elif (run_dir / "agent_output.txt").exists() or (run_dir / "conversation.jsonl").exists():
        outcome = "AGENT_TIMEOUT_OR_INCOMPLETE"
    else:
        outcome = "NEVER_ATTEMPTED"
    queue = {
        "RATE_LIMITED": "rate-limited",
        "ENVIRONMENT_ERROR": "infrastructure",
        "HARNESS_ERROR": "infrastructure",
        "ARTIFACT_REDACTION_ERROR": "infrastructure",
        "AGENT_TIMEOUT_OR_INCOMPLETE": "agent-timeout",
        "NEVER_ATTEMPTED": "infrastructure",
    }.get(outcome, "evaluator")
    return {"task_id": task_id, "run": run_dir.name, "outcome": outcome, "queue": queue,
           "attempt_count": attempts, "next_eligible_at": time.time() + _BACKOFF_S * max(1, attempts)}


def build_queue(system=None):
    system = system or config.ASTRA_OPENBOOK_SYSTEM_NAME
    lock = p.load_lock()
    items = []
    for task_id, run_dir in _run_dirs(system):
        item = _classify(task_id, run_dir)
        if item:
            items.append(item)
    return {
        "generated_at": time.time(),
        "system": system,
        "lock_population_sha256": lock["population"]["sha256"],
        "lock_status": lock.get("status"),
        "items": items,
    }


def main(argv=None):
    print(json.dumps(build_queue(), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
