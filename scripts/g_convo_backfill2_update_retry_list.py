"""Recompute the retry list for the round-2 conversation backfill (scripts/g_convo_backfill2_ids.txt):
tasks whose 3 fresh run_N/ don't ALL have a real (tool_use-bearing) conversation.jsonl -- a run
that hit the subscription rate limit writes a 1-turn stub transcript that `transcript_saved` alone
can't distinguish from a genuine capture (see agent_computer._save_conversation_transcript).

Restricted to tasks the driver has already produced all 3 result.json for -- an in-flight or
not-yet-reached task is not a retry candidate, it just hasn't run yet. Idempotent: safe to call
repeatedly against a backfill still in progress (see g_convo_backfill2_retry_watcher.sh).

    python -m scripts.g_convo_backfill2_update_retry_list
"""
import json
import pathlib

RESULTS = pathlib.Path("benchmarks/osworld/results/agent_computer")
IDS_FILE = pathlib.Path("scripts/g_convo_backfill2_ids.txt")
RETRY_FILE = pathlib.Path("scripts/g_convo_backfill2_retry_ids.txt")

# This backfill's own launch (2026-09-04T09:02:20Z, scripts/g_convo_backfill2_driver.log line 1).
# Every one of these 289 tasks already had a `run_N/` from G3 or an earlier arm BEFORE this
# backfill started -- a plain "does result.json exist" check counts that stale run as done and
# judges retry-worthiness from a transcript (or its absence) that predates this effort entirely.
# started_at is what actually distinguishes "this backfill's attempt" from everything before it.
BACKFILL_STARTED_AT = "2026-09-04T09:02:20"


def _real_transcript(path):
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


def main():
    ids = IDS_FILE.read_text().split()
    task_dirs = {d.name: d for d in RESULTS.iterdir() if d.is_dir()}

    def _touched_by_this_backfill(run_dir):
        """True if THIS backfill made an attempt at this run slot, successful or not.

        A run whose provisioning fails (Daytona timeout, 502 Bad Gateway, ...) is caught by
        core.run's own except clause, which writes infra_error.json and returns *before*
        agent_computer.run ever gets called -- result.json is never touched, so the stale
        pre-backfill result.json (or its total absence, for a slot that never completed even
        in the original G3) is all that's on disk. Checking result.json's started_at alone
        misses these attempts entirely and undercounts what still needs a retry."""
        rj = run_dir / "result.json"
        if rj.exists():
            try:
                rec = json.loads(rj.read_text())
                started = (rec.get("provenance") or {}).get("started_at") or ""
                if started >= BACKFILL_STARTED_AT:
                    return True
            except json.JSONDecodeError:
                pass
        ie = run_dir / "infra_error.json"
        if ie.exists():
            try:
                history = json.loads(ie.read_text())
                if any((entry.get("at") or "") >= BACKFILL_STARTED_AT for entry in history):
                    return True
            except json.JSONDecodeError:
                pass
        return False

    attempted, needs_retry = 0, []
    for tid in ids:
        td = task_dirs.get(tid)
        if td is None:
            continue
        run_dirs = [td / f"run_{k}" for k in (1, 2, 3)]
        if not all(_touched_by_this_backfill(rd) for rd in run_dirs):
            continue  # not all 3 runs attempted by THIS backfill yet -- in flight or not reached
        attempted += 1
        n_real = sum(_real_transcript(rd / "conversation.jsonl") for rd in run_dirs)
        if n_real < 3:
            needs_retry.append(tid)

    RETRY_FILE.write_text("\n".join(needs_retry) + ("\n" if needs_retry else ""))
    print(f"[retry-list] {attempted}/{len(ids)} task processati -- "
          f"{len(needs_retry)} da riprovare -> {RETRY_FILE}")


if __name__ == "__main__":
    main()
