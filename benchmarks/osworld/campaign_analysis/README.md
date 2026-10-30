# OSWorld campaign analysis

Cross-arm analysis of every OSWorld campaign (old Phase-1 and new official-protocol infrastructure; Claude Sonnet 5 and gpt-6-astra), produced 2026-10-14 after the campaigns were interrupted. The summary below covers the two latest campaigns, the official-protocol ones; everything else is in the local documentation listed at the end.

## Summary: the two official-protocol campaigns

Protocol: OSWorld-Verified (361 tasks = 369 minus 8 login tasks), one `computer` tool, 1080p, 100 steps, official evaluators at upstream commit `091f5ef`, 5 runs per task, Daytona guest image pkgfix2. Main campaign trees: `agent_computer_sonnet5_effortmax_protocol361img3_official` (Claude Sonnet 5, effort max, Claude Code) and `agent_computer_gpt6astra_max_codex01534_protocol361img3_official` (gpt-6-astra, reasoning max, Codex CLI 0.153.4). Both were stopped by subscription quota before covering the population.

An *informative* run is a SUCCESS/FAILURE verdict produced by the agent: runs without a verdict (stopped on quota), runs killed by the agent CLI (not logged in, session limit) or the API, evaluator/environment errors and no-op passes (the checker accepts the initial state) are excluded.

### How much was replicated

| | Sonnet 5 | gpt-6-astra |
|---|---|---|
| tasks tried / of 361 | 274 (76%) | 169 (47%) |
| tasks with at least one informative verdict | 261 (72%) | 155 (43%) |
| run directories | 1,370 | 845 |
| informative runs | 1,288 | 749 |
| excluded runs | 82: 42 no verdict (quota), 26 CLI-killed, 9 evaluator errors, 5 environment errors | 96: 87 no verdict (quota), 5 no-op passes, 3 environment/evaluator errors, 1 API-killed |
| **pass rate over informative runs** | **71.3%** (918/1,288) | **72.8%** (545/749); 71.4% (535/749) before the clock correction |
| tasks all-pass / mixed / all-fail | 149 / 57 / 55 | 95 / 27 / 33 |

Clock correction (H1, 27/10): Codex exposes a clock tool that cannot be disabled, and the official tool-surface rule scored 22 Astra runs FAILURE for calling it, without evaluating them; the clock is now exempt (calls recorded) and the 20 clock-voided runs were re-run in their own tree (10 pass), the other 2 voids stay. The tables below keep the original counts.

The two pass rates are not the protocol's pass@1 over 361 × 5 runs (coverage 72% and 43% of the tasks) and are not a replication of the published numbers; on the tasks both models completed they are equal within noise.

### Where the failures are (by application)

| application | Sonnet 5: tasks · pass rate · failed runs · tasks with a failure (all-fail) | gpt-6-astra: tasks · pass rate · failed runs · tasks with a failure (all-fail) |
|---|---|---|
| LibreOffice Calc | 63 · 79.4% · 65 · 19 (8) | 46 · 88.7% · 26 · 7 (4) |
| Chrome | 55 · 68.0% · 87 · 21 (16) | 45 · 64.3% · 80 · 23 (10) |
| LibreOffice Impress | 49 · 78.7% · 52 · 21 (4) | 21 · 74.1% · 22 · 9 (3) |
| LibreOffice Writer | 34 · 56.7% · 71 · 19 (11) | not run |
| GIMP | 27 · 67.9% · 43 · 13 (7) | 26 · 73.4% · 34 · 9 (6) |
| OS (terminal, settings) | 19 · 70.2% · 28 · 11 (4) | 14 · 26.1% · 51 · 11 (10) |
| Thunderbird | 6 · 76.9% · 6 · 2 (2) | not run |
| VS Code | 5 · 87.5% · 3 · 3 (0) | not run |
| VLC | 2 · 0.0% · 10 · 2 (2) | 3 · 92.3% · 1 · 1 (0) |
| other | 1 · 0.0% · 5 · 1 (1) | — |
| **total** | **261 · 71.3% · 370 · 112 (55)** | **155 · 71.4% · 214 · 60 (33)** |

### Why runs fail (failed informative runs)

| how the run failed | Sonnet 5 | gpt-6-astra |
|---|---|---|
| claimed done, the checker scored 0 | 293 (79%) | 101 (47%) |
| declared FAIL / [INFEASIBLE] on a feasible task | 20 (5%), of which 19 with an environment factor | 73 (34%), of which 61 with an environment factor (black desktop 29, site blocked 23, desktop-tool error 9) |
| hit the 100-step cap | 49 (13%), 6 of them on a black desktop | 0 |
| scored FAILURE by the tool-surface rule (Codex clock tool on relative-date tasks) | — | 22 (10%) |
| answered in text without acting, or declined | 5 (1%) | 13 (6%) |
| checker rejects an output equivalent up to letter case | 3 (1%) | 5 (2%) |

The dominant mode for both models is "claimed done, checker 0"; Astra gives up more on feasible tasks (and is almost always right on the infeasible-by-design ones: 63 of 64), Sonnet 5 runs out of steps more.

### Root causes of Astra's failures (task-by-task analysis)

Every Astra task with a failure was read by hand (transcript, final screenshot, saved output against the gold). Failed informative runs of the main campaign by dominant cause:

| cause | tasks | failed runs | share of informative runs |
|---|---|---|---|
| F — bare desktop (no GNOME Shell/Settings), missing dependency, or a reading of the task that led the agent to give up | 11 | 45 | 6.0 pp |
| G — checker strictness, brittle checker, or ambiguous task | 11 | 41 | 5.5 pp |
| I — genuine agent error (environment and checker working) | 14 | 38 | 5.1 pp |
| H — setting applied in the GUI but not registered where the checker reads it (persistence) | 7 | 35 | 4.7 pp |
| D — live site blocking the sandbox's datacenter address | 6 | 28 | 3.7 pp |
| E — tool-surface rule (clock tool) | 5 | 12 | 1.6 pp |
| J — unexplained or mixed | 2 | 8 | 1.1 pp |
| C — infrastructure (desktop-tool errors) | 4 | 7 | 0.9 pp |
| **total** | **60** | **214** | **28.6 pp** |

Only about 5 of the 28.6 failure points are genuine agent errors (class I: 38 runs, 5.1 points; 7.7 points if the class-F runs where the agent missed an available route are added: no `gsettings` on three settings tasks, 15 runs, and give-ups that other runs avoid, 5 runs); the rest is shared between the environment (F, H, D, C), the protocol rule (E) and the benchmark's checkers (G). A live check of the guest (2026-10-19) found that 6 of the 7 class-H tasks and 2 of the class-F tasks are unsolvable for any agent on this image (40 failed runs, 5.3 points): the image redirects application configuration (`XDG_CONFIG_HOME`) away from where the checkers read it, and lacks `bc` and the GNOME Shell schema. A corrected guest layer (`docker/Dockerfile.osworld-pkgfix3`) fixes the first two; re-running gpt-6-astra on the seven affected tasks it had tried (separate tree, 35 runs) gives **25/35 passes against 0/35** on the original image: five tasks go from 0/5 to 5/5, `d52d6308` stays 0/5 because its checker demands all docks hidden while the instruction asks for the left one, and `3ce045a0` stays 0/5 because Astra, on a now feasible task, never tries `gsettings`. Re-running Claude Sonnet 5 on the same corrected image (34 tasks × 5 runs: the 8 hit by the configuration redirect and the 26 where failed runs left the checked file untouched) gives **130/170 against 58/168** on the original image: the configuration-redirect tasks go from 0/40 to 34/40 and the save-dialog tasks from 45% to 74%. About 72 more Sonnet 5 runs pass on these tasks alone, roughly 5.6 points of its 1,288 informative runs; the main-campaign numbers above are not recomputed with these re-runs. Typical agent errors: a pivot table placed one row too low, "hide" done with conditional formatting, a chart missing from the saved file, slide numbers coloured on the slides but not on the master, two cookies left after a deletion.

Infrastructure defects found on the official-protocol guest (details in the local documentation): bare Openbox desktop; application configuration written under `/opt/osw_xdg_unused` instead of `~/.config` (GIMP, LibreOffice); LibreOffice's "Confirm File Format" dialog blocking the official save postconfig; `bc` and the GNOME Shell schema missing; `DISPLAY=:99` in the guest against `:0` in the vendored official prompt; GIMP configuration and Writer save tasks that pass in Phase 1 and fail here for both models; live sites refusing the datacenter IP.

## Files

- `campaign_analysis.py` — generator: reads the archived result trees and writes the per-run table, the per-task pages, `SUMMARY.md` and `tables/`; inputs `manual_notes.json` (hand-written task notes) and `verification.json` (hand verification of labels).
- `make_figures.py` and `figures/` (PDF and PNG) — ten figures from the per-run table (pass rate by arm, run composition, categories, failure modes, environment factors, infrastructure flips, time and cost, consistency, checkers, quota friction).
- `tables/` — task × arm pass matrix with hand notes, timing, per-bucket metrics, failures by checker.
- `astra_ledger.json` — per-task data of the Astra failure analysis (behaviour, reason, certainty, label).

## Documentation index (local only)

Every `.md` here except this README is thesis material, ignored by git and mirrored every 15 minutes to the external disk (`thesis-live-backup/`). Reading order and role of each:

| document | role |
|---|---|
| `FINDINGS.md` | start here: results of every campaign and the verified causes of failure, with the conclusions of the re-runs |
| `BUG_REGISTER.md` | the single list of defects (guest image, harness, external, benchmark, agent) with evidence, affected tasks, owner and status; other documents cite its codes (G1, G2, …) |
| `EXPERIMENTS.md` | one record per experiment: hypothesis, setup, data, results, reading (pkgfix3 image, the two re-runs, Phase 3 baseline and arms) |
| `ASTRA_ANALYSIS.md` | task-by-task analysis of gpt-6-astra's failures (99 tasks, classes A–J; reclassifications after the live check in §7b) |
| `ASTRA_IMPROVEMENT_RESEARCH.md` | how to improve the agent: failure pools, literature, candidate interventions, the Phase 3 decision (§6; the superseded first proposal is in Appendix A) |
| `METHODOLOGY.md` | data, arms, definitions, failure taxonomy, label verification, limits |
| `SUMMARY.md` | every aggregate table generated by `campaign_analysis.py` |
| `RESEARCH_BRIEF_FOR_LLM.md`, `RESEARCH_REPORT_FROM_LLM.md` | brief given to an external research LLM and its report, verbatim (verdicts on its claims in `ASTRA_IMPROVEMENT_RESEARCH.md` §6.10) |

The working plan for closing Phase 2 and running Phase 3 is in the repository's `docs/osworld-plan-phase2-close-phase3.md` (also local).

What is **not** in git (size): the raw results and full transcripts, the per-run table (3,770 rows) and one cross-arm page per task (373 pages, with per-run evidence). They live in the external archive `agentic-qa-archive-20261014/` (`analysis_campaigns/` next to the raw trees); the Astra pkgfix3 re-run tree is in its `osworld-experiments/` folder, and the later trees (Sonnet 5 re-run, Phase 3 arms) are mirrored to `thesis-live-backup/osworld-results/` on the same disk. Regenerate with
`python campaign_analysis.py <archive_root> <out_dir> manual_notes.json verification.json` (dependencies in `../requirements.txt`).
