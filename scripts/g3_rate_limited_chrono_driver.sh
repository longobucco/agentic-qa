#!/bin/bash
# Sequentially re-runs the G3-full tasks that have a pending rate-limited attempt,
# processed in chronological order of when they were first rate-limited.
# Each invocation only executes the (task, run) units still missing eval.json
# (run.py skips already-done runs unless --force).
set -u
cd /Users/lucavisconti/QATesting/agentic-qa
export OSW_TASK_TIMEOUT=2700
export OSW_PROVISION_TIMEOUT=600
export OSW_POST_RUN_TIMEOUT=600

IDS_FILE="/private/tmp/claude-501/-Users-lucavisconti-QATesting-agentic-qa/1be412d0-af19-49f6-997c-e7d2d054a07b/scratchpad/rate_limited_chrono_ids.txt"
i=0
total=$(wc -l < "$IDS_FILE" | tr -d ' ')

while IFS= read -r tid; do
  i=$((i+1))
  echo "=== [$i/$total] $tid ==="
  .venv/bin/python3 -m benchmarks.osworld.run --system agent_computer --ids "$tid" --runs 3
done < "$IDS_FILE"

echo "=== chronological rate-limited re-run driver: all $total tasks processed ==="
