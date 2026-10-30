# OSWorld-Verified: replication and harness interventions

Computer-use agents on OSWorld-Verified (369 tasks on a real Ubuntu desktop; xlang-ai/OSWorld at
commit `091f5ef`), run through commercial agent CLIs on a cloud sandbox. Two models: Claude
Sonnet 5 (Claude Code, `claude -p`) and GPT-6-Astra (Codex CLI 0.153.4, `codex exec`).

**Research questions.** (1) Can the published OSWorld-Verified results be reproduced on a cloud
sandbox with the official protocol? (2) What do the failures actually depend on: the agent, the
environment, or the benchmark? (3) Which changes to the agent's harness, at a fixed model, improve
the outcome without harming tasks the agent already solves or must refuse?

## Protocol

| element | setting |
|---|---|
| population | OSWorld-Verified minus the 8 tasks that need a personal login: 361 tasks |
| observation / action | 1920×1080 screenshots; one MCP tool, `computer`, translated with upstream's own action parser |
| budget | 100 steps, 4 h per task; 5 runs per task (Phase 2), 3 or 5 (Phase 3, per task set) |
| instructions | upstream Claude system prompt, vendored verbatim (`official_protocol.py`) |
| termination | upstream rule: `[INFEASIBLE]` or a fail action → FAIL, otherwise DONE |
| scoring | official evaluators at `091f5ef`; the task set, the evaluators and the guest controller are pinned to that commit |
| isolation | empty working directory; no user settings, hooks, plugins or memory; built-in CLI tools denied; pinned CLI versions; any tool other than `computer` ends the run as FAILURE |

### Deviations from the official setup

| deviation | reason | effect |
|---|---|---|
| Daytona container (`docker/Dockerfile.osworld`) instead of the official QEMU VM | no KVM host available | guest differences, corrected in layers below; residual ones are flagged per task |
| image layers `pkgfix` (26/09), `pkgfix2` (28/09) | packages and the D-Bus session address that tasks call in their own setup | Phase 2 runs on `pkgfix2` |
| image layer `pkgfix3` (19/10) | configuration redirected away from `~/.config`, LibreOffice save dialog, missing `bc`: tasks unsolvable for any agent | Phase 3 runs on `pkgfix3` only, in separate trees |
| Codex read-only MCP-resource functions exempt from the tool-surface rule | cannot be disabled and cannot reach the guest | recorded per run as `agent_exempt_tool_calls` |
| Codex clock tool exempt (H1, correction of 27/10) | cannot be disabled; the rule voided 22 runs without scoring them | the 20 clock-voided runs re-run in their own tree (`..._clockexempt_official`) |
| live websites | some refuse the sandbox's datacenter address | not corrected (part of the benchmark); counted as environment-caused failures |

## Definitions

- **Verdict.** `eval.json` holds SUCCESS or FAILURE from the official evaluator. Other outcomes are
  `ENVIRONMENT_ERROR` (task setup failed), `EVAL_ERROR` (scoring failed; `evaluate.py` never guesses
  a verdict) and, in `infra_error.json`, `RATE_LIMITED` / `INFRA_FLAKE` / `HARNESS_ERROR`, which
  are retried and never scored.
- **Informative run.** A verdict produced by the agent. Excluded: runs without a verdict, runs
  killed by the CLI or the API, evaluator and environment errors, and no-op passes (the checker
  accepts the untouched initial state). All pass rates below are over informative runs.
- **Incidental SUCCESS.** A SUCCESS whose agent did not finish cleanly (`agent_clean_finish=False`):
  reported separately by `report.py`.

## Results

**Phase 2: replication with the official protocol** (28/09–14/10/2026, guest `pkgfix2`, main trees
`..._protocol361img3_official`). Both campaigns were stopped by subscription quota before covering
the population, so these are not pass@1 over 361 × 5 runs.

| | Claude Sonnet 5 | GPT-6-Astra |
|---|---|---|
| tasks with an informative verdict | 261 / 361 | 155 / 361 |
| informative runs | 1,288 | 749 |
| pass rate | 71.3% (918) | 72.8% (545) |

The Astra figure includes the clock correction; with the original rule it is 71.4% (535/749).
On the corrected guest, the tasks hit by the image defects go from 0/35 to 25/35 passes (Astra)
and from 58/168 to 130/170 (Sonnet 5); a null agent that only answers DONE passes 7 of 350 tasks,
which are flagged in every analysis. Failure analysis, figures and tables: `campaign_analysis/`.

**Phase 3: harness interventions** (from 21/10/2026, guest `pkgfix3`). Each arm only adds text or
one rule to the official protocol, runs in its own results tree, records the hash of what it added
in every run, and is compared task by task with A0 on the same sets. Task sets, with their
selection rules, are in `data/phase3_sets.json`; criteria are fixed before the runs.

| arm | adds to the official protocol | task sets |
|---|---|---|
| `a0` | nothing (baseline on `pkgfix3`) | classF, infeasible, controls, classP, heldoutH |
| `e1card` | capability card: the desktop is minimal, settings are reachable from a terminal, probe before `[INFEASIBLE]` | classF, infeasible, controls |
| `e1bounce` | the card, plus one bounce of an `[INFEASIBLE]` given without a probe | classF, infeasible, controls |
| `e1card2` | the card with the infeasibility rule closed (no workarounds after a confirmed gap) | classF, infeasible, controls |
| `e1b` | the card plus rules on how results are checked | classP, heldoutH (held out), controls, infeasible |

Results of Phase 3 are reported once each experiment is complete.

## Layout

| path | role |
|---|---|
| `runners/agent_computer.py`, `runners/gpt_astra.py` | Claude Code and Codex runners; `common.py` and `astra_common.py` hold shared primitives (MCP config, telemetry, provenance, scoring) |
| `runners/null_agent.py` | null-agent control: setup and scoring with no action |
| `official_protocol.py` | vendored upstream system prompt and termination rule |
| `arms.py`, `phase3.py` | Phase 3 arm texts; experiment table and launcher |
| `mcp/` | MCP server exposing the single `computer` tool (`_upstream_actions.py` is vendored) |
| `env/` | Daytona sandbox, controller client, official evaluator bridge, loopback/CDP relays |
| `config.py` | env-derived configuration; refuses non-official values; results-tree naming; image pins |
| `campaign.py`, `campaign_daytona.py` | Phase 2 campaign driver core and its Daytona backend (`scripts/g_official361_driver.py`) |
| `report.py`, `evaluate.py`, `tasks.py`, `benchmark.py`, `run.py` | report, scoring fallback, task loading, runner wiring, entry point |
| `data/` | download scripts for the pinned tasks and evaluators; `phase3_sets.json` |
| `docker/` | guest image and its layers (`pkgfix`, `pkgfix2`, `pkgfix3`) |
| `campaign_analysis/` | offline analysis of the archived campaigns (generator, figures, tables) |
| `analysis/` | screenshot-delivery probe (do the CLIs pass the full 1080p frame?) |
| `tests/` | offline unit tests (no sandbox, no model) |
| `astra_official361_lock.json` | frozen Astra campaign lock, checked by the preflight |

## Reproduction

```
pip install -r benchmarks/osworld/requirements.txt
python -m benchmarks.osworld.data.download_data
python -m benchmarks.osworld.data.download_evaluators      # mandatory: pinned evaluators
python -m pytest benchmarks/osworld/tests core/tests
```

`DAYTONA_API_KEY` goes in the repository `.env`; Codex must be logged in for Astra. The guest image
is pinned by digest in `config.py`; `OSW_IMAGE` is needed only for a rebuilt image (a tree suffix
with its own image, such as `pkgfix3`, selects it and refuses any other).

```
# one task, Claude Sonnet 5, official protocol
OSW_MODEL=claude-sonnet-5 python -m benchmarks.osworld.run --ids <task-id>
# Phase 2 campaign (361 × 5, resumable, quota-aware; Astra also needs OSW_ASTRA_REASONING_EFFORT)
BACKEND=daytona ARM=sonnet|astra PARALLEL=K MAX_HOURS=H python scripts/g_official361_driver.py
# Phase 3: list the declared groups, run one (resumes from disk)
python -m benchmarks.osworld.phase3 list
python -m benchmarks.osworld.phase3 run astra e1card2 classF
# null agent on the pkgfix3 guest
OSW_SYSTEM_SUFFIX=pkgfix3 python -m benchmarks.osworld.run --system agent_computer_nullagent_pkgfix3_official
# report (pass rate, official mean reward, incidental successes, tool-surface violations)
python -m benchmarks.osworld.report <tree>
python -m benchmarks.osworld.report --compare <tree-a> <tree-b>
```

Results land in `results/<tree>/<task>/run_<k>/`: `result.json` (with `provenance`: image digest,
evaluator commit, model, effort, arm and its text hash, harness commit), `eval.json`,
`agent_output.txt`, `final.png`; transcripts and raw artefacts stay local.

## Data and code availability

- Git tracks the code, the task sets and the analysis outputs; Phase 1 code is frozen under the tag
  `phase1-daytona-frozen`, the Phase 2 harness under `phase2-official361-daytona-frozen`.
- Raw results, full transcripts and the per-run analysis tables are archived outside the repository
  (byte-for-byte verified); the Phase 3 trees are mirrored there continuously.
- Every run records its harness commit from 30/10/2026; earlier runs are matched to the code by
  their timestamps against the git history.

## Notes

- The task set, the guest controller and the evaluators are bumped together (`data/download_data.py`,
  `docker/Dockerfile.osworld`, `data/download_evaluators.py`).
- Images are pinned by digest: Daytona caches by tag, so a mutable tag can serve a stale build.
- Reusing a desktop across tasks (`OSW_CONTROLLER_URL`, `OSW_SANDBOX_ID`) does not reset state;
  such runs are for debugging, not for pass rates.
- Chrome and VS Code run through wrappers (`--no-sandbox`, explicit user-data directory), as both
  refuse to start as root.
- Daytona may stop a sandbox early and never restarts its controller; `env/sandbox.py` handles both
  (`python -m benchmarks.osworld.env.sandbox resume <id>`).
