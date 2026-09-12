#!/bin/bash
# Verify-Replan pilot (docs/verify-replan-minimal-integration-plan.md Section 13): 4 arms x 30
# frozen task ids x 3 runs = 360 runs. NOT run automatically by this repo -- this script costs
# real sandbox/API spend and is the explicit next-approval checkpoint after the branch's own
# offline test suite (see benchmarks/osworld/README.md's Verify-Replan status line).
#
# Arms:
#   1. baseline          -- agent_computer_sonnet5, natural OSW_MAX_TURNS=150
#   2. audit-only        -- verify_replan_sonnet5_auditonly (OSW_VR_MAX_RECOVERIES=0): measures
#                            the auditor alone, no recovery ever attempted
#   3. verify-replan     -- verify_replan_sonnet5 (OSW_VR_MAX_RECOVERIES=1): the real treatment
#   4. matched-budget baseline -- agent_computer_sonnet5 again, OSW_MAX_TURNS bumped to the
#      treatment's worst-case total (100 initial + 12 audit + 38 recovery + 12 final audit = 162),
#      so a pass-rate delta isn't just "the treatment got to spend more turns" (Section 3.4).
#      This SHARES arm 1's results tree (agent_computer_sonnet5) -- there is no separate config
#      knob for a second agent_computer variant, so arm 1's own run_N/ is copied aside to
#      run_N_natural_budget_legacy/ before arm 4 overwrites it with --force. Same
#      preserve-before-overwrite discipline used throughout this project's other ablations
#      (see run_N_pre_enforcement_legacy/, run_N_generic_nudge_pilot_legacy/ elsewhere in
#      results/) -- never a silent overwrite of one arm's data by another's.
set -u
cd /Users/lucavisconti/QATesting/agentic-qa

MANIFEST="benchmarks/osworld/verify_replan_pilot_manifest.json"
EXPECTED_SHA256="baf523b6b304484fc3140f33ac0c6b4bfc4cdce749606854db4a574cd0942761"
EXPECTED_COUNT=30
LOG="scripts/run_verify_replan_sonnet5_pilot.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)  $*" | tee -a "$LOG"; }

if [ ! -f "$MANIFEST" ]; then
  log "ERROR: manifest not found: $MANIFEST -- run: python -m scripts.build_verify_replan_pilot_manifest"
  exit 2
fi
actual_sha256=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['sha256'])")
actual_count=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['count'])")
if [ "$actual_sha256" != "$EXPECTED_SHA256" ] || [ "$actual_count" -ne "$EXPECTED_COUNT" ]; then
  log "ERROR: pilot manifest changed since this driver was written (sha256=$actual_sha256, count=$actual_count) -- re-review before running a differently-scoped pilot under the same script"
  exit 2
fi
IDS=$(python3 -c "import json; print(' '.join(json.load(open('$MANIFEST'))['all_ids']))")

export OSW_MODEL=claude-sonnet-5

log "=== Arm 1/4: baseline (agent_computer_sonnet5, natural budget) ==="
python3 -m benchmarks.osworld.run --system agent_computer_sonnet5 --ids $IDS --runs 3
status=$?
[ "$status" -ne 0 ] && log "!!! arm 1 runner exited $status -- artifacts remain resumable"

log "=== Arm 2/4: audit-only (verify_replan_sonnet5_auditonly, recovery disabled) ==="
OSW_VR_MAX_RECOVERIES=0 python3 -m benchmarks.osworld.run \
  --system verify_replan_sonnet5_auditonly --ids $IDS --runs 3
status=$?
[ "$status" -ne 0 ] && log "!!! arm 2 runner exited $status -- artifacts remain resumable"

log "=== Arm 3/4: verify-replan (verify_replan_sonnet5, recovery enabled) ==="
python3 -m benchmarks.osworld.run --system verify_replan_sonnet5 --ids $IDS --runs 3
status=$?
[ "$status" -ne 0 ] && log "!!! arm 3 runner exited $status -- artifacts remain resumable"

log "=== Preserving arm 1's natural-budget results before arm 4 overwrites the same tree ==="
for tid in $IDS; do
  for n in 1 2 3; do
    src="benchmarks/osworld/results/agent_computer_sonnet5/$tid/run_$n"
    dst="benchmarks/osworld/results/agent_computer_sonnet5/$tid/run_${n}_natural_budget_legacy"
    if [ -d "$src" ] && [ ! -d "$dst" ]; then
      cp -R "$src" "$dst"
    fi
  done
done

log "=== Arm 4/4: matched-budget baseline (agent_computer_sonnet5, OSW_MAX_TURNS=162, --force) ==="
OSW_MAX_TURNS=162 python3 -m benchmarks.osworld.run \
  --system agent_computer_sonnet5 --ids $IDS --runs 3 --force
status=$?
[ "$status" -ne 0 ] && log "!!! arm 4 runner exited $status -- artifacts remain resumable"

log "=== Pilot complete: 4 arms x $actual_count tasks x 3 runs. Analyze per Section 13's endpoints before any stop/go decision (Section 13's own criteria) or scaling to the full population. ==="
