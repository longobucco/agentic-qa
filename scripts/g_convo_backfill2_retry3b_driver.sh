#!/bin/bash
# One-off driver for the 2026-09-04 conversation-transcript backfill, round 2: 289 tasks with no real
# Adapted from g3_full_overnight_driver.sh's per-task timeout/backoff pattern, scoped down to a
# fixed 50-id list instead of the full campaign backlog -- no watchdog relaunch needed here,
# this is a bounded one-shot batch, not a multi-day unattended campaign.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600
PER_TASK_TIMEOUT=4500   # 75min hard ceiling per task (3 runs), same as g3_full_overnight_driver.sh

IDS_FILE="scripts/g_convo_backfill2_retry3b_ids.txt"
LOG="scripts/g_convo_backfill2_retry3b_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

total=$(wc -l < "$IDS_FILE" | tr -d ' ')
log "=== conversation-backfill batch: $total task(s) ==="
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
    log "$tid (up to 3 runs) finished in ${task_elapsed}s -- looks session-limited, backing off 300s"
    sleep 300
  fi
done < "$IDS_FILE"
log "=== batch complete ==="
