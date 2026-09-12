#!/bin/bash
# Consumes a GENERATED retry queue (scripts/build_astra_open_book_retry_queue.py) -- never a
# hand-edited id list, per the spec's own requirement. Regenerates the queue itself on every
# invocation so it always reflects the current results tree, then retries only items whose
# next_eligible_at has passed and whose queue is retryable without human investigation
# (rate-limited, infrastructure once readiness is fixed, agent-timeout under a bounded policy).
# `evaluator` queue items are deliberately skipped: the spec calls for investigating the
# evaluator, never a blind retry.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

LOCK="benchmarks/osworld/astra_openbook_campaign_lock.json"
LOG="scripts/g_astra_open_book_retry_driver.log"
QUEUE_FILE="scripts/g_astra_open_book_retry_queue.json"
MAX_AGENT_TIMEOUT_RETRIES=2

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

lock_status=$(.venv/bin/python3 -c "import json; print(json.load(open('$LOCK'))['status'])")
if [ "$lock_status" != "frozen" ]; then
  log "ERROR: $LOCK status=$lock_status, not 'frozen' -- refusing to retry against a draft lock"
  exit 2
fi

.venv/bin/python3 -m scripts.build_astra_open_book_retry_queue > "$QUEUE_FILE"

queue_lock_sha256=$(.venv/bin/python3 -c "import json; print(json.load(open('$QUEUE_FILE'))['lock_population_sha256'])")
lock_sha256=$(.venv/bin/python3 -c "import json; print(json.load(open('$LOCK'))['population']['sha256'])")
if [ "$queue_lock_sha256" != "$lock_sha256" ]; then
  log "ERROR: retry queue was built for a different lock ($queue_lock_sha256 != $lock_sha256)"
  exit 2
fi

now=$(date +%s)
mapfile -t retryable < <(.venv/bin/python3 -c "
import json, time
q = json.load(open('$QUEUE_FILE'))
now = time.time()
max_timeout_retries = $MAX_AGENT_TIMEOUT_RETRIES
for item in q['items']:
    if item['next_eligible_at'] > now:
        continue
    if item['queue'] == 'evaluator':
        continue
    if item['queue'] == 'agent-timeout' and item['attempt_count'] > max_timeout_retries:
        continue
    print(item['task_id'])
" | sort -u)

total=${#retryable[@]}
log "=== GPT Astra open-book retry: $total task(s) eligible ==="
i=0
for tid in "${retryable[@]}"; do
  i=$((i + 1))
  log "--- [$i/$total] $tid ---"
  .venv/bin/python3 -m benchmarks.osworld.run \
    --system agent_computer_astra_openbook --ids "$tid" --runs 3
  status=$?
  if [ "$status" -ne 0 ]; then log "!!! $tid runner exited $status"; fi
done
log "=== GPT Astra open-book retry complete ==="
