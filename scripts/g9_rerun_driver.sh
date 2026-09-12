#!/bin/bash
# G9 re-run pass: the tasks whose Sonnet-5 verdicts were decided by a defect on OUR side, redone
# now that the defects are fixed (benchmarks/osworld/docs/finding-sonnet5-oracle-http-scheme.md).
#
# Why this is a separate driver and not extra ids on the recovery one: every task here already
# has an eval.json, and is_done() treats that as complete -- the recovery pass demonstrated it
# live on e135df7c ("nothing to run ... moving on immediately"). The old runs are archived to
# run_N_pre_g9fix_legacy first (core/results.py's *_legacy convention: retire tainted runs
# without losing them, and is_run_dir() keeps analysis from picking them back up), which also
# makes --force unnecessary -- so an interruption resumes instead of redoing finished work.
#
# The id list is regenerated at start rather than frozen: the recovery pass keeps writing while
# this waits, so the honest list is the one true at launch. It is also written to disk, so what
# ran is reviewable afterwards.
#
# Deliberately NOT included: scripts/g9_probe_first_ids.txt -- 4 tasks whose EVAL_ERROR can't yet
# be attributed to us or to the agent. Re-running those proves nothing until a live no-op probe
# says which it is.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

export OSW_MODEL=claude-sonnet-5
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600
PER_TASK_TIMEOUT=4500

IDS_FILE="scripts/g9_rerun_after_fix_ids.txt"
LOG="scripts/g9_rerun_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

# One sandbox per task, concurrency_safe=False: never overlap with another driver.
while pgrep -f "g_sonnet5_recovery_driver.sh" > /dev/null; do
  log "waiting for the recovery driver to finish ..."
  sleep 120
done

log "recovery driver done -- regenerating the re-run list"
.venv/bin/python3 -m benchmarks.osworld.analysis.g9_replication_validity \
  --system agent_computer_sonnet5 --write-ids "$IDS_FILE" 2>&1 | tee -a "$LOG"

total=$(wc -l < "$IDS_FILE" | tr -d ' ')
log "=== G9 re-run pass: $total task(s), model=$OSW_MODEL, evaluator pinned ==="
i=0
while IFS= read -r tid; do
  [ -z "$tid" ] && continue
  i=$((i+1))
  log "--- [$i/$total] $tid ---"

  # Archive the tainted runs once. If the legacy copy already exists this task was archived on
  # an earlier pass -- leave whatever is in run_N now, which is the good re-run, not the old one.
  td="benchmarks/osworld/results/agent_computer_sonnet5/$tid"
  for k in 1 2 3; do
    if [ -d "$td/run_$k" ] && [ ! -d "$td/run_${k}_pre_g9fix_legacy" ]; then
      mv "$td/run_$k" "$td/run_${k}_pre_g9fix_legacy"
      log "    archived run_$k -> run_${k}_pre_g9fix_legacy"
    fi
  done

  task_start=$(date +%s)
  gtimeout -k 60 "$PER_TASK_TIMEOUT" \
    .venv/bin/python3 -m benchmarks.osworld.run --ids "$tid" --runs 3
  status=$?
  task_elapsed=$(( $(date +%s) - task_start ))
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    log "!!! $tid hit the ${PER_TASK_TIMEOUT}s hard ceiling (exit $status) -- force-killed, skipping"
  fi
  pkill -9 -f "claude -p You are an autonomous agent" 2>/dev/null

  if [ "$task_elapsed" -lt 5 ]; then
    log "$tid -- nothing to run (already complete per is_done), moving on immediately"
  elif [ "$task_elapsed" -lt 60 ]; then
    log "$tid (up to 3 runs) finished in ${task_elapsed}s -- looks session-limited, backing off 300s"
    sleep 300
  fi
done < "$IDS_FILE"
log "=== batch complete ==="
