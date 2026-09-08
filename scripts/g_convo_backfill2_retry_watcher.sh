#!/bin/bash
# Keeps scripts/g_convo_backfill2_retry_ids.txt up to date while g_convo_backfill2_driver.sh
# runs: every 3 minutes, recompute which of the 289 tasks it has already redone in full still
# lack a real (non-rate-limited) transcript on all 3 runs. Exits on its own once the driver
# process is gone, after one final recompute.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
DRIVER_PID=58436
LOG="scripts/g_convo_backfill2_retry_watcher.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

while kill -0 "$DRIVER_PID" 2>/dev/null; do
  .venv/bin/python3 -m scripts.g_convo_backfill2_update_retry_list 2>&1 | while IFS= read -r line; do log "$line"; done
  sleep 180
done
log "--- driver (pid $DRIVER_PID) is gone, one final recompute ---"
.venv/bin/python3 -m scripts.g_convo_backfill2_update_retry_list 2>&1 | while IFS= read -r line; do log "$line"; done
