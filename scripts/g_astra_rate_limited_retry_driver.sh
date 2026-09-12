#!/bin/bash
# Resume only Astra tasks that have RATE_LIMITED attempts and no official verdict.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

export OSW_ASTRA_MODEL=gpt-6-astra
export OSW_ASTRA_REASONING_EFFORT=high
export OSW_ASTRA_CODEX_VERSION=0.153.4
export OSW_ENFORCE_SANDBOX=1
export OSW_RESTRICT_RUN_PYTHON=1
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600

IDS_FILE=scripts/g_astra_rate_limited_retry_ids.txt
LOG=scripts/g_astra_rate_limited_retry_driver.log
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }
ids() { grep -v '^[[:space:]]*#' "$IDS_FILE" | sed '/^[[:space:]]*$/d'; }

total=$(ids | wc -l | tr -d ' ')
if [ "$total" -ne 17 ] || [ "$(ids | sort | uniq -d | wc -l | tr -d ' ')" -ne 0 ]; then
  log "ERROR: retry manifest must contain exactly 17 unique IDs"
  exit 2
fi

log "=== GPT Astra RATE_LIMITED retry: $total task(s) ==="
i=0
while IFS= read -r tid; do
  i=$((i + 1))
  log "--- [$i/$total] $tid ---"
  marker=$(mktemp)
  # --force is intentional: the previous eval.json files were generated while
  # codex-code-mode-host was missing and are invalid environment-only attempts.
  .venv/bin/python3 -m benchmarks.osworld.run --system agent_computer_astra --ids "$tid" --runs 3 --force
  status=$?
  if [ "$status" -ne 0 ]; then log "!!! $tid runner exited $status"; fi
  if find "benchmarks/osworld/results/agent_computer_astra/$tid" -path '*/run_*/infra_error.json' -newer "$marker" -type f -exec grep -l 'RATE_LIMITED' {} + | grep -q .; then
    log "$tid hit RATE_LIMITED; backing off 300s"
    rm -f "$marker"
    sleep 300
    continue
  fi
  rm -f "$marker"
done < <(ids)
log "=== GPT Astra RATE_LIMITED retry complete ==="
