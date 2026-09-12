#!/bin/bash
# Sonnet-5 campaign: the first run of this benchmark with the model actually PINNED
# (OSW_MODEL), after an audit found the G3 campaign had silently spanned three models. Writes
# to results/agent_computer_sonnet5/ -- a separate tree, so the historical mixed-model results
# (many of whose runs have no saved transcript) are never touched.
#
# 299 tasks: the full in-scope population minus the 59 chrome ones, app-stratified/interleaved
# so an interruption still leaves a balanced sample. No --force: is_done() lets this resume
# after any stop without redoing completed runs, because this tree starts empty and every run
# in it belongs to this campaign.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

export OSW_MODEL=claude-sonnet-5
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600
PER_TASK_TIMEOUT=4500

IDS_FILE="scripts/g_sonnet5_final_recovery_ids.txt"
LOG="scripts/g_sonnet5_recovery_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

total=$(wc -l < "$IDS_FILE" | tr -d ' ')
log "=== sonnet-5 recovery pass: $total task(s), model=$OSW_MODEL ==="
i=0
while IFS= read -r tid; do
  i=$((i+1))
  log "--- [$i/$total] $tid ---"
  task_start=$(date +%s)
  gtimeout -k 60 "$PER_TASK_TIMEOUT" \
    .venv/bin/python3 -m benchmarks.osworld.run --ids "$tid" --runs 3
  status=$?
  task_elapsed=$(( $(date +%s) - task_start ))
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    log "!!! $tid hit the ${PER_TASK_TIMEOUT}s hard ceiling (exit $status) -- force-killed, skipping"
  fi
  pkill -9 -f "claude -p You are an autonomous agent" 2>/dev/null

  # A rate-limited attempt still provisions and calls the CLI, so it takes ~30-60s; an exit in
  # a couple of seconds means core.run found nothing to do (is_done() saw eval.json for all 3
  # runs) and never touched the API. Backing off 300s for that wastes five minutes on a task
  # that was already complete, so only the slow-but-short case is treated as throttling.
  if [ "$task_elapsed" -lt 5 ]; then
    log "$tid -- nothing to run (already complete per is_done), moving on immediately"
  elif [ "$task_elapsed" -lt 60 ]; then
    log "$tid (up to 3 runs) finished in ${task_elapsed}s -- looks session-limited, backing off 300s"
    sleep 300
  fi
done < "$IDS_FILE"
log "=== batch complete ==="
