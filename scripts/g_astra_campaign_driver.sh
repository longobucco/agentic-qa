#!/bin/bash
# GPT Astra replication over the frozen 299-task campaign manifest. It retains the 202-task
# Sonnet-5 paired base and adds the documented 97-task Astra expansion; it is not the full
# 358-task runnable population.
# Results are isolated under results/agent_computer_astra/. No --force: completed runs resume.
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

IDS_FILES=("scripts/g_sonnet5_campaign_ids.txt" "scripts/g_astra_campaign_expansion_ids.txt")
EXCLUDED_IDS_FILE="scripts/g_astra_campaign_google_drive_excluded_ids.txt"
IDS_SHA256="0a8cfe80ea8921ac183d45bfb4375fef14c129d99ef421ff92d93496adc17659"
LOG="scripts/g_astra_campaign_driver.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

ids_stream() {
  cat "${IDS_FILES[@]}" | grep -v '^[[:space:]]*#' | sed '/^[[:space:]]*$/d' |
    grep -vxF -f <(grep -v '^[[:space:]]*#' "$EXCLUDED_IDS_FILE" | sed '/^[[:space:]]*$/d')
}
total=$(ids_stream | wc -l | tr -d ' ')
if [ "$total" -ne 291 ]; then
  log "ERROR: Astra manifest changed: expected 291 task ids, found $total"
  exit 2
fi
duplicates=$(ids_stream | sort | uniq -d | wc -l | tr -d ' ')
if [ "$duplicates" -ne 0 ]; then
  log "ERROR: Astra manifest contains $duplicates duplicate task id(s)"
  exit 2
fi
actual_sha256=$(ids_stream | shasum -a 256 | cut -d ' ' -f 1)
if [ "$actual_sha256" != "$IDS_SHA256" ]; then
  log "ERROR: Astra manifest hash changed: expected $IDS_SHA256, found $actual_sha256"
  exit 2
fi
log "=== GPT Astra replication: $total task(s), model=$OSW_ASTRA_MODEL, effort=$OSW_ASTRA_REASONING_EFFORT, codex=$OSW_ASTRA_CODEX_VERSION ==="
mkdir -p benchmarks/osworld/results/agent_computer_astra
.venv/bin/python3 -m benchmarks.osworld.closed_book > benchmarks/osworld/results/agent_computer_astra/open_book_audit.json
i=0
while IFS= read -r tid; do
  i=$((i+1))
  log "--- [$i/$total] $tid ---"
  book=$(.venv/bin/python3 -m benchmarks.osworld.closed_book --id "$tid")
  if [ "$book" = "open-book" ]; then
    log "$tid tagged open-book -- deferred to a dedicated future campaign"
    continue
  fi
  task_start=$(date +%s)
  .venv/bin/python3 -m benchmarks.osworld.run \
    --system agent_computer_astra --ids "$tid" --runs 3
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
done < <(ids_stream)
log "=== GPT Astra replication complete ==="
