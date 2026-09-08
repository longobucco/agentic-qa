#!/bin/bash
# Same pattern as g_convo_backfill2_retry_watcher.sh, aimed at the retry pass (pid 38661,
# scripts/g_convo_backfill2_retry3_driver.sh): recomputes which of the 189 retried tasks
# still lack a real transcript on all 3 runs, every 3 minutes, exiting after one final
# recompute once the retry driver itself is gone.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
DRIVER_PID=38661
LOG="scripts/g_convo_backfill2_retry3_watcher.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

while kill -0 "$DRIVER_PID" 2>/dev/null; do
  .venv/bin/python3 -m scripts.g_convo_backfill2_retry3_update_still_missing 2>&1 | while IFS= read -r line; do log "$line"; done
  sleep 180
done
log "--- retry driver (pid $DRIVER_PID) is gone, one final recompute ---"
.venv/bin/python3 -m scripts.g_convo_backfill2_retry3_update_still_missing 2>&1 | while IFS= read -r line; do log "$line"; done
