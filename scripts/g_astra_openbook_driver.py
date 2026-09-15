"""Population-scale driver for the open-book GPT Astra campaign
(agent_computer_astra_openbook): runs the full curated population (258 tasks,
astra_openbook_campaign_lock.json's own population) in small batches, with automatic
resume and a rate-limit backoff -- neither existed before this script; every batch run this
session so far was a manual `--ids <a handful>` invocation.

Why batches, not one giant --ids call: each task attempt provisions a REAL Daytona sandbox
(guest proxy, egress lockdown, etc.) BEFORE the agent's own Codex call can even hit a 429 --
confirmed live 2026-09-13/14, a sustained OpenAI/Codex rate-limit window turned 7 of 27 tasks in
one batch into RATE_LIMITED infra failures, each still paying full sandbox-provisioning cost for
zero signal. Running the population as one call has no place to insert a pause when that
happens; this driver checks the rate-limited fraction after each batch and backs off
(exponential, capped) before the next one, instead of continuing to hammer a known-down API.

Resumable for free, not via its own state file: every batch is just
`python -m benchmarks.osworld.run --system agent_computer_astra_openbook --ids <batch> --runs N`
(no --force), and core.run's own is_done() check already skips any (task, run_idx) unit that
already has a scored eval.json -- restarting this script after a kill just re-walks the same
batches and skips whatever already finished. RATE_LIMITED units are the deliberate exception:
they never produce an eval.json, so a later pass naturally retries them.

ENVIRONMENT_ERROR/EVAL_ERROR units are a DIFFERENT exception, in the other direction: they DO
write an eval.json (an inconclusive one, per core/reporting.py's own _INCONCLUSIVE_VERDICTS), so
is_done() treats them as finished forever, even one caused by a transient infra bug that gets
fixed later in the same campaign (confirmed live 2026-09-15: a bundle-push read timeout, fixed
in env/controller.py, would otherwise have left that task's run stuck on the pre-fix verdict).
After the main batch pass, this driver does one extra pass: finds every task with such a
verdict on any run and force-retries it once (--force re-does every run index for that task,
not just the stuck one -- --ids has no per-run-index granularity). Pass --no-retry-inconclusive
to skip this if you want the raw, unre-tried batch results instead.

Requires the campaign lock to be status="frozen" (this script does not freeze it -- that
remains a deliberate, separate decision each time a real campaign launch is intended) and
OSW_OPENBOOK_IMAGE set in the environment; both are enforced by the existing
open_book_preflight.campaign_check()/config.OPENBOOK_IMAGE checks the runner already has, not
duplicated here.

    OSW_OPENBOOK_IMAGE=ghcr.io/... python -m scripts.g_astra_openbook_driver \\
        --runs 3 --batch-size 8

    # Resume after an interruption: identical invocation, already-scored units are skipped.

Writes a running JSON summary to the path given by --summary-out (default
scripts/g_astra_openbook_driver_state.json) after every batch, so progress can be checked
without waiting for the whole population to finish.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.osworld import config, open_book_preflight

_ROOT = Path(__file__).resolve().parents[1]
_RESULTS_DIR = _ROOT / "benchmarks" / "osworld" / "results" / config.ASTRA_OPENBOOK_SYSTEM_NAME


def _population_ids():
    lock = open_book_preflight.load_lock()
    return sorted(open_book_preflight._population_ids(lock))


def _batches(ids, size):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _run_dirs_for(task_id):
    tdir = _RESULTS_DIR / task_id
    if not tdir.exists():
        return []
    return [d for d in tdir.iterdir() if d.is_dir() and d.name.startswith("run_")]


def _latest_infra_outcome(run_dir):
    """The last infra_error.json entry written for this run-dir, or None if it never hit one
    (a clean scored run, or one that hasn't been attempted at all)."""
    path = run_dir / "infra_error.json"
    if not path.exists():
        return None
    try:
        records = json.loads(path.read_text())
        return records[-1].get("outcome") if records else None
    except Exception:
        return None


def _batch_rate_limited_fraction(batch_ids, before_mtimes):
    """Fraction of this batch's run-dirs whose infra_error.json was WRITTEN OR MODIFIED during
    this batch's invocation and whose latest outcome is RATE_LIMITED -- `before_mtimes` (from
    immediately before the subprocess call) distinguishes a fresh rate-limit hit from a stale
    leftover file from some earlier, unrelated attempt."""
    total = 0
    rate_limited = 0
    for task_id in batch_ids:
        for run_dir in _run_dirs_for(task_id):
            path = run_dir / "infra_error.json"
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            key = str(run_dir)
            if mtime <= before_mtimes.get(key, 0):
                continue  # untouched by this batch
            total += 1
            if _latest_infra_outcome(run_dir) == "RATE_LIMITED":
                rate_limited += 1
    return (rate_limited / total) if total else 0.0


def _snapshot_infra_mtimes(batch_ids):
    out = {}
    for task_id in batch_ids:
        for run_dir in _run_dirs_for(task_id):
            path = run_dir / "infra_error.json"
            out[str(run_dir)] = path.stat().st_mtime if path.exists() else 0
    return out


def _write_summary(path, state):
    path.write_text(json.dumps(state, indent=2))


_INCONCLUSIVE_VERDICTS = {"ENVIRONMENT_ERROR", "EVAL_ERROR"}


def _eval_verdict(run_dir):
    path = run_dir / "eval.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("verdict")
    except Exception:
        return None


def _tasks_with_an_inconclusive_run(ids):
    """core.run's own is_done() treats ANY eval.json (verdict and all) as 'done' -- an
    ENVIRONMENT_ERROR/EVAL_ERROR from a transient infra hiccup (e.g. the read-timeout fixed
    2026-09-15) is otherwise stuck forever, never retried by a later resumed pass, even after
    the underlying bug is fixed. Returns task ids (not run-dir paths: --force + --ids only takes
    task ids, so a retry re-does every run index for that task) that have at least one such
    verdict among their existing run-dirs."""
    out = []
    for task_id in ids:
        for run_dir in _run_dirs_for(task_id):
            if _eval_verdict(run_dir) in _INCONCLUSIVE_VERDICTS:
                out.append(task_id)
                break
    return out


def main(argv=None):
    # Confirmed live 2026-09-15: under nohup (stdout redirected to a file, not a TTY), Python
    # fully buffers stdout by default -- this driver's own print() calls (batch/backoff markers)
    # sat unflushed for long stretches while core.run's subprocess output (writing straight to
    # the same fd, not through this buffer) appeared immediately, making the log look like batch
    # boundaries and backoff events were missing or badly out of order. Line-buffering here fixes
    # that without touching subprocess.run's own output.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # not a real stdout (e.g. captured by a test runner) -- nothing to reconfigure

    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--base-backoff-s", type=float, default=60.0)
    ap.add_argument("--max-backoff-s", type=float, default=1800.0)
    ap.add_argument("--rate-limit-threshold", type=float, default=0.5,
                    help="if this fraction or more of a batch's fresh infra outcomes are "
                         "RATE_LIMITED, back off before the next batch")
    ap.add_argument("--summary-out", default=str(_ROOT / "scripts" /
                                                 "g_astra_openbook_driver_state.json"))
    ap.add_argument("--no-retry-inconclusive", action="store_true",
                    help="skip the final pass that force-retries tasks stuck on an "
                         "ENVIRONMENT_ERROR/EVAL_ERROR verdict (see _tasks_with_an_inconclusive_run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the batch plan without invoking core.run")
    args = ap.parse_args(argv)

    if not config.OPENBOOK_IMAGE:
        sys.exit("OSW_OPENBOOK_IMAGE not set -- required for every open-book run, see "
                 "config.OPENBOOK_IMAGE / env/sandbox.py's own check")

    ids = _population_ids()
    batches = list(_batches(ids, args.batch_size))
    summary_path = Path(args.summary_out)
    state = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "population_size": len(ids), "batch_size": args.batch_size, "runs": args.runs,
        "total_batches": len(batches), "batches_done": 0,
        "backoff_events": [], "last_batch_rate_limited_fraction": None,
    }

    if args.dry_run:
        print(json.dumps({"population_size": len(ids), "total_batches": len(batches),
                          "first_batch": batches[0] if batches else []}, indent=2))
        return

    backoff = args.base_backoff_s
    for i, batch in enumerate(batches):
        before = _snapshot_infra_mtimes(batch)
        cmd = [sys.executable, "-m", "benchmarks.osworld.run",
              "--system", config.ASTRA_OPENBOOK_SYSTEM_NAME,
              "--runs", str(args.runs), "--ids", *batch]
        print(f"[batch {i + 1}/{len(batches)}] {len(batch)} task(s): {' '.join(batch)}", flush=True)
        result = subprocess.run(cmd, cwd=_ROOT)
        if result.returncode != 0:
            print(f"[batch {i + 1}/{len(batches)}] core.run exited {result.returncode} -- "
                 f"continuing to the next batch regardless (per-task failures are expected; "
                 f"this only matters if EVERY batch does it, e.g. a lock/preflight problem)")

        fraction = _batch_rate_limited_fraction(batch, before)
        state["batches_done"] = i + 1
        state["last_batch_rate_limited_fraction"] = fraction
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        if fraction >= args.rate_limit_threshold:
            state["backoff_events"].append({
                "batch": i + 1, "fraction_rate_limited": fraction,
                "sleeping_s": backoff, "at": datetime.now(timezone.utc).isoformat(),
            })
            _write_summary(summary_path, state)
            print(f"[batch {i + 1}/{len(batches)}] {fraction:.0%} of this batch's fresh infra "
                 f"outcomes were RATE_LIMITED -- backing off {backoff:.0f}s before continuing")
            time.sleep(backoff)
            backoff = min(backoff * 2, args.max_backoff_s)
        else:
            backoff = args.base_backoff_s  # a clean batch resets the backoff

        _write_summary(summary_path, state)

    if not args.no_retry_inconclusive:
        stuck = _tasks_with_an_inconclusive_run(ids)
        state["inconclusive_retry"] = {"tasks": stuck, "count": len(stuck)}
        _write_summary(summary_path, state)
        if stuck:
            print(f"\n{len(stuck)} task(s) have an ENVIRONMENT_ERROR/EVAL_ERROR verdict on at "
                 f"least one run -- is_done() would otherwise leave them stuck forever, even "
                 f"after an infra fix. Force-retrying once: {' '.join(stuck)}")
            retry_batches = list(_batches(stuck, args.batch_size))
            for i, batch in enumerate(retry_batches):
                cmd = [sys.executable, "-m", "benchmarks.osworld.run",
                      "--system", config.ASTRA_OPENBOOK_SYSTEM_NAME,
                      "--force", "--runs", str(args.runs), "--ids", *batch]
                print(f"[inconclusive-retry {i + 1}/{len(retry_batches)}] "
                     f"{len(batch)} task(s): {' '.join(batch)}")
                subprocess.run(cmd, cwd=_ROOT)
            state["inconclusive_retry"]["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_summary(summary_path, state)

    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_summary(summary_path, state)
    print(f"\nDone: {len(batches)} batch(es) over {len(ids)} task(s) x {args.runs} run(s). "
         f"Summary: {summary_path}")
    print("Run `python -m benchmarks.osworld.report agent_computer_astra_openbook` for the "
         "aggregate pass rate.")


if __name__ == "__main__":
    main()
