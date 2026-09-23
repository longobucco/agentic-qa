#!/bin/bash
# Protocol-aligned OSWorld-Verified campaigns (docs/superpowers/plans/2026-09-24-osworld-protocol-alignment.md).
# Usage: ARM=sonnet|astra PHASE=A|B [MAX_HOURS=48] scripts/g_protocol361_driver.sh
#   PHASE A: current MCP toolset.  PHASE B: + zoom and batch (own results trees).
set -u
cd "$(dirname "$0")/.."

export OSW_POPULATION=verified361
export OSW_MAX_STEPS=100
export OSW_MAX_TURNS=100
export OSW_SCREEN_WIDTH=1920
export OSW_SCREEN_HEIGHT=1080
RUNS=5
MAX_HOURS="${MAX_HOURS:-48}"
BACKOFF_S=1800   # same 30-min poll as the open-book driver: quota resets are hours apart

case "${PHASE:-}" in
  A) export OSW_ZOOM_BATCH=0; LOCK=astra_protocol361_lock.json ;;
  B) export OSW_ZOOM_BATCH=1; LOCK=astra_protocol361_zoombatch_lock.json ;;
  *) echo "set PHASE=A or PHASE=B" >&2; exit 2 ;;
esac

case "${ARM:-}" in
  sonnet)
    # Same toolset as Codex: no run_python, no host shell/web (core/codex_loop.py disables them).
    export OSW_MODEL=claude-sonnet-5 OSW_EFFORT=max OSW_MAX_OUTPUT_TOKENS=128000
    export OSW_RESTRICT_RUN_PYTHON=1 OSW_ENFORCE_SANDBOX=1
    export OSW_SYSTEM_SUFFIX=protocol361
    SYSTEM=$(.venv/bin/python -c "from benchmarks.osworld import config; print(config.SYSTEM_NAME)") ;;
  astra)
    : "${OSW_ASTRA_REASONING_EFFORT:?set the Astra campaign effort explicitly}"
    export OSW_ASTRA_REASONING_EFFORT OSW_ASTRA_SYSTEM_SUFFIX=protocol361
    export OSW_ASTRA_CAMPAIGN_LOCK="$LOCK"
    SYSTEM=$(.venv/bin/python -c "from benchmarks.osworld import config; print(config.ASTRA_SYSTEM_NAME)") ;;
  *) echo "set ARM=sonnet or ARM=astra" >&2; exit 2 ;;
esac

LOG="scripts/g_protocol361_${ARM}_${PHASE}.log"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }
remaining() {
  .venv/bin/python - "$SYSTEM" "$RUNS" <<'EOF'
import sys
from benchmarks.osworld import config, tasks
from core import results
system, runs = sys.argv[1], int(sys.argv[2])
print(sum(1 for t in tasks.load_tasks() for k in range(1, runs + 1)
          if not results.is_done(results.run_dir(config.RESULTS_DIR, system, t["id"], k))))
EOF
}

log "=== start ARM=$ARM PHASE=$PHASE SYSTEM=$SYSTEM runs=$RUNS max_hours=$MAX_HOURS ==="
deadline=$(( $(date +%s) + MAX_HOURS * 3600 ))
prev=$(remaining)
log "remaining runs: $prev"
while [ "$prev" -gt 0 ] && [ "$(date +%s)" -lt "$deadline" ]; do
  .venv/bin/python -m benchmarks.osworld.run --system "$SYSTEM" --runs "$RUNS" 2>&1 | tee -a "$LOG"
  now=$(remaining)
  log "remaining runs: $now (was $prev)"
  if [ "$now" -ge "$prev" ]; then
    log "no progress (rate limit / quota) -- backing off ${BACKOFF_S}s"
    sleep "$BACKOFF_S"
  fi
  prev=$now
done
log "=== done: remaining=$prev ==="
