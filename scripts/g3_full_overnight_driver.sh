#!/bin/bash
# Persistent overnight driver for the G3-full campaign.
#
# Loops indefinitely, one task at a time (so run order is exact -- core.tasks' interleave-by-
# bucket step reshuffles a multi-id batch, so strict ordering across tasks only holds when each
# invocation gets a single id). Every pass recomputes the order fresh via
# `g3_full_watchdog.py --print-order`: tasks with a pending rate-limited attempt go first
# (earliest rate-limit timestamp first), then the rest of the backlog -- so a unit that gets
# freshly rate-limited this pass jumps the queue on the very next one instead of waiting to
# cycle back around.
#
# A single task (up to 3 runs) that finishes in well under a minute means every run in it hit
# the same instant wall (the Claude subscription's session limit -- rate-limited attempts fail
# in ~1 turn, no real agent work, agent_api_error_status=429 in the written result.json) rather
# than genuine progress (median genuine run alone is minutes, per core.run's own timings); back
# off right there before hammering the API on the remaining 100+ tasks in the list. Checked per
# TASK, not per pass (observed live 2026-08-14: a 127-task pass with EVERY unit rate-limited
# still averaged ~40s/task -- 3 sequential subprocess spin-ups add up -- so a pass-level average
# never dropped below a "still hammering" threshold and the backoff never fired for the whole
# pass; a lone task stuck at ~40-60s total is unambiguous, a 127-task pass average is not).
#
# Meant to be started once and left running all night. If it dies unexpectedly (crash, unhandled
# error), or gets stuck without dying (see below), the launchd watchdog (g3_full_watchdog.py, now
# checked every ~10min -- see its _STALE_LOG_S) restarts it -- no manual resume needed.
#
# Observed live (2026-08-14): a macOS sleep/wake cycle broke a claude subprocess's network
# connection AND (it looks like) froze its own internal TASK_TIMEOUT clock -- the SIGKILL that
# should have fired at 45min never did, and the unit sat hung for 4h42m. Python-level timeouts
# are not trustworthy on a machine that sleeps, so every per-task invocation is ALSO wrapped in
# an OS-level `gtimeout` as a second, independent enforcement path: 3 runs/task * ~65min worst
# case each (provision 10min + agent 45min + post-run capture/score 10min) = 195min, so 200min
# with a minute of kill grace. `caffeinate` (started alongside this driver) should prevent the
# sleep/wake cycle that caused this in the first place, but the hard ceiling stays regardless --
# belt and suspenders, matching sandbox.py's own reasoning for _PROVISION_TIMEOUT_S.
#
# gtimeout kills COMMAND's own process group (the default without --foreground), which covers
# run.py and any plain children -- but the `claude` subprocess is spawned with
# start_new_session=True (core.agent_loop), i.e. its OWN session/group, so gtimeout's group-kill
# does not reach it. The explicit pkill after every task invocation is what actually guarantees
# no orphaned `claude -p` process survives into the next iteration.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600
PER_TASK_TIMEOUT=12000   # 200min hard ceiling, OS-enforced regardless of Python's own clock

IDS_FILE=$(mktemp)
trap 'rm -f "$IDS_FILE"' EXIT

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*"; }

while true; do
  .venv/bin/python3 scripts/g3_full_watchdog.py --print-order > "$IDS_FILE"
  total=$(wc -l < "$IDS_FILE" | tr -d ' ')

  if [ "$total" -eq 0 ]; then
    log "G3-full: nothing left pending -- campaign complete"
    break
  fi

  log "=== new pass: $total task(s) pending, rate-limited-first ==="
  pass_start=$(date +%s)
  i=0
  while IFS= read -r tid; do
    i=$((i+1))
    log "--- [$i/$total] $tid ---"
    task_start=$(date +%s)
    gtimeout -k 60 "$PER_TASK_TIMEOUT" \
      .venv/bin/python3 -m benchmarks.osworld.run --system agent_computer --ids "$tid" --runs 3
    status=$?
    task_elapsed=$(( $(date +%s) - task_start ))
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      log "!!! $tid hit the ${PER_TASK_TIMEOUT}s hard ceiling (exit $status) -- force-killed, will retry next pass"
    fi
    # gtimeout can't reach the claude subprocess (its own session -- see header); make sure
    # nothing from this task survives into the next iteration regardless of how it exited.
    pkill -9 -f "claude -p You are an autonomous agent" 2>/dev/null

    if [ "$task_elapsed" -lt 60 ]; then
      log "$tid (up to 3 runs) finished in ${task_elapsed}s -- looks session-limited, backing off 300s"
      sleep 300
    fi
  done < "$IDS_FILE"
  log "=== pass complete in $(( $(date +%s) - pass_start ))s ==="
done
