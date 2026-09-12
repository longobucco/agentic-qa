#!/bin/bash
# GPT Astra open-book campaign driver. Mirrors g_astra_campaign_driver.sh's structure (frozen-
# manifest hash check, resumable loop, rate-limit backoff) against
# astra_openbook_campaign_lock.json instead of the closed-book lock.
#
# Refuses to run at all while the lock's status is not "frozen" -- open_book_preflight.py's own
# campaign_check() enforces this too (core.run calls it as this system's preflight hook before
# any task runs), but failing here as well means a stale/draft lock never even starts spinning
# up a sandbox.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

LOCK="benchmarks/osworld/astra_openbook_campaign_lock.json"
IDS_FILE="scripts/g_astra_openbook_ids.txt"
LOG="scripts/g_astra_open_book_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

lock_status=$(.venv/bin/python3 -c "import json; print(json.load(open('$LOCK'))['status'])")
if [ "$lock_status" != "frozen" ]; then
  log "ERROR: $LOCK status=$lock_status, not 'frozen' -- refusing to run against a draft lock"
  exit 2
fi

expected_count=$(.venv/bin/python3 -c "import json; print(json.load(open('$LOCK'))['population']['count'])")
expected_sha256=$(.venv/bin/python3 -c "import json; print(json.load(open('$LOCK'))['population']['sha256'])")
ids() { grep -v '^[[:space:]]*#' "$IDS_FILE" | sed '/^[[:space:]]*$/d'; }
total=$(ids | wc -l | tr -d ' ')
if [ "$total" -ne "$expected_count" ]; then
  log "ERROR: open-book manifest changed: lock expects $expected_count task ids, found $total"
  exit 2
fi
actual_sha256=$(ids | shasum -a 256 | cut -d ' ' -f 1)
if [ "$actual_sha256" != "$expected_sha256" ]; then
  log "ERROR: open-book manifest hash changed: expected $expected_sha256, found $actual_sha256"
  exit 2
fi

log "=== GPT Astra open-book campaign: $total task(s) ==="
i=0
while IFS= read -r tid; do
  i=$((i + 1))
  log "--- [$i/$total] $tid ---"
  task_start=$(date +%s)
  .venv/bin/python3 -m benchmarks.osworld.run \
    --system agent_computer_astra_openbook --ids "$tid" --runs 3
  status=$?
  task_elapsed=$(( $(date +%s) - task_start ))
  if [ "$status" -ne 0 ]; then
    log "!!! $tid runner exited $status -- artifacts remain resumable"
  fi
  if [ "$task_elapsed" -lt 5 ]; then
    log "$tid -- already complete, moving on"
  elif [ "$task_elapsed" -lt 60 ]; then
    log "$tid finished unusually quickly -- possible usage limit, backing off 300s"
    sleep 300
  fi
done < <(ids)
log "=== GPT Astra open-book campaign complete ==="
