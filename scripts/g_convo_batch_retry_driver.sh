#!/bin/bash
# Corrective second pass for the 2026-08-25 conversation-transcript backfill batch: the 27
# tasks (26 rate-limited stubs + 1 partial) left over from g_convo_batch_driver.sh's first
# pass -- see scripts/g_convo_batch_retry_ids.txt for the list and the balance in
# g_convo_batch_driver.log for how each was classified. Backoff bumped 300s -> 600s: the
# first pass hit a sustained rate-limit stretch (tasks 33-46, 14 in a row) where 300s wasn't
# enough to clear the window.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600
PER_TASK_TIMEOUT=4500

IDS_FILE="scripts/g_convo_batch_retry_ids.txt"
LOG="scripts/g_convo_batch_retry_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

total=$(wc -l < "$IDS_FILE" | tr -d ' ')
log "=== conversation-backfill RETRY pass: $total task(s) ==="
i=0
while IFS= read -r tid; do
  i=$((i+1))
  log "--- [$i/$total] $tid ---"
  task_start=$(date +%s)
  gtimeout -k 60 "$PER_TASK_TIMEOUT" \
    .venv/bin/python3 -m benchmarks.osworld.run --system agent_computer --ids "$tid" --runs 3 --force
  status=$?
  task_elapsed=$(( $(date +%s) - task_start ))
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    log "!!! $tid hit the ${PER_TASK_TIMEOUT}s hard ceiling (exit $status) -- force-killed, skipping"
  fi
  pkill -9 -f "claude -p You are an autonomous agent" 2>/dev/null

  if [ "$task_elapsed" -lt 60 ]; then
    log "$tid (up to 3 runs) finished in ${task_elapsed}s -- looks session-limited, backing off 600s"
    sleep 600
  fi
done < "$IDS_FILE"
log "=== retry pass complete ==="
