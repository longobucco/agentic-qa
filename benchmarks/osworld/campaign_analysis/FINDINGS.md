# OSWorld campaigns — findings

Scope: every result tree of the OSWorld-Verified replication, split into six arms (see `METHODOLOGY.md`): Claude Sonnet 4.6, Claude Sonnet 5 and gpt-6-astra on the old (Phase 1) infrastructure, plus Sonnet 5 and gpt-6-astra on the new (official-protocol) one. 4,844 run directories, 375 task ids. Numbers are per run unless stated; "informative" runs exclude runs the agent CLI never completed and no-op passes (definition in `METHODOLOGY.md`). Every claim carries an evidence level: **verified** = read in transcripts, screenshots, diffs or task configs; **suspected** = consistent with the data but not reproduced in the VM.

## 1. Headline

| arm | tasks tried | run dirs | recorded pass% | informative runs | **informative pass%** | tasks with a verdict |
|---|---|---|---|---|---|---|
| sonnet46-old (Sonnet 4.6, Phase 1) | 358 | 1,074 | 50.8% | 745 | **50.7%** | 253 |
| sonnet5-old (Sonnet 5, Phase 1) | 300 | 900 | 56.8% | 833 | **56.8%** | 280 |
| astra-old (gpt-6-astra, Phase 1, closed-book) | 53 | 157 | 36.2% | 132 | **40.9%** | 46 |
| astra-old-openbook (small) | 79 | 191 | 43.9% | 45 | 60.0% | 23 |
| sonnet5-new (official protocol) | 274 | 1,370 | 70.2% | 1,288 | **71.3%** | 261 |
| astra-new (official protocol) | 169 | 845 | 71.5% | 749 | **71.4%** | 155 |

None of these is the protocol's pass@1 over 361 tasks × 5 runs: coverage is 261/361 tasks (Sonnet 5 new) and 155/361 (Astra new), and the old arms ran different subsets. On tasks both arms completed (per-task mean score):

| comparison | common tasks | A | B | A−B |
|---|---|---|---|---|
| sonnet5-old vs sonnet5-new | 195 | 65.3% | 72.1% | −6.8 pp |
| astra-old vs astra-new | 20 | 20.0% | 42.0% | −22.0 pp |
| sonnet5-new vs astra-new | 141 | 75.0% | 75.6% | −0.6 pp |
| sonnet5-old vs astra-old | 44 | 43.2% | 43.2% | 0.0 pp |
| sonnet46-old vs sonnet5-old | 236 | 52.5% | 57.5% | −5.0 pp |
| sonnet46-old vs sonnet5-new | 187 | 54.9% | 74.5% | −19.6 pp |

Per task, on the new infrastructure (141 tasks both models completed): solved by both 109, only Sonnet 5 5, only Astra 8, neither 19. On the old infrastructure (66 tasks): both 27, Claude only 16, Codex only 4, neither 19.

**Reading.** On the same infrastructure the two models are indistinguishable (71.3% vs 71.4%; 75.0% vs 75.6% on common tasks). The large differences are between infrastructures, and they are not only gains: 37 tasks flip from 0 to ≥66% when moving to the new infrastructure, 7 flip the other way (section 3.4). The old→new difference therefore mixes model/harness changes with environment fixes and regressions; it is not a clean model comparison.

## 2. The recorded numbers are not all agent verdicts (verified)

Reading transcripts and file timestamps showed that the harness recorded verdicts that say nothing about the agent:

1. **Transcripts overwritten (genuine verdict, behaviour lost).** 283 sonnet46-old and 8 sonnet5-old runs have a valid `eval.json` and `final.png` (August/early September) but a transcript that is a single "You've hit your session limit" message written weeks later by a back-fill retry. Example: task 1273e544, run_1 — the final screenshot shows `Sheet2` with the copied Revenue column, verdict SUCCESS, transcript = quota message. They count as normal verdicts; the causes of their 129 failures cannot be read from transcripts.
2. **CLI aborted but scored.** 45 sonnet46-old, 26 sonnet5-new ("Not logged in · Please run /login" 19, session/weekly limit 7), 17 astra-old and 19 astra-old-openbook runs (Codex "You've hit your usage limit", "Selected model is at capacity") were recorded as FAILURE although the agent did not run or was killed mid-run. They are excluded from the informative numbers; this moves astra-old from 36.2% to 40.9% and the open-book arm from 43.9% to 60.0%.
3. **No-op passes.** 12 runs (5 astra-new on task 3299584d, 5 sonnet46-old, 2 open-book) pass with zero tool calls: the checker accepts the initial state.
4. **Evaluator and setup errors (Phase 1).** 152 of sonnet46-old's 1,074 runs are `EVAL_ERROR` (96 are `JSONDecodeError` from the evaluator getters, others `AttributeError`/`TypeError` in the pinned `desktop_env`) and 76 `ENVIRONMENT_ERROR` (24 Google Drive "invalid client secrets", 27 Chrome CDP connection failures). Sonnet5-old: 18 + 27 (24 Google Drive). These tasks have no verdict.
5. **Rate limits dominate the new campaigns' logs.** `infra_error.json` holds 6,998 `RATE_LIMITED` retry records for Astra new and 3,679 for Sonnet 5 new; 87 and 42 runs never produced a verdict.

## 3. Why runs fail

Failure modes (share of FAILURE verdicts; one run = one mode):

| mode | sonnet46-old | sonnet5-old | astra-old | sonnet5-new | astra-new |
|---|---|---|---|---|---|
| claimed done, checker 0 | 55.0% | 87.2% | 22.1% | 75.3% | 47.0% |
| declared FAIL / [INFEASIBLE] | 3.5% | 11.7% | 60.0% | 5.1% | 34.0% |
| hit step/turn cap | 4.9% | — | — | 12.6% | — |
| harness tool-surface rule | — | — | — | — | 10.2% |
| text answer / refusal, 0 actions | 0.8% | 1.1% | — | 1.3% | 6.1% |
| checker rejects equivalent output (case only) | — | — | — | 0.8% | 2.3% |
| transcript lost (cause unknown) | 34.8% | — | — | — | — |
| API-aborted (infra) | — | — | 17.9% | — | 0.5% |
| CLI-aborted (infra) | 1.1% | — | — | 4.9% | — |

Environment factors present when an agent gave up or hit the cap (share of FAILURE runs): black desktop 53.7% (astra-old), 13.5% (astra-new), 5.3% (sonnet5-old), 3.1% (sonnet5-new); live site blocked 10.7% (astra-new), 1.5% (sonnet5-new); missing precondition 5.8% (sonnet5-old); desktop-tool HTTP errors 16.2% (astra-old-openbook), 4.2% (astra-new).

The dominant mode everywhere is "claimed done, checker 0". What lies behind it differs by infrastructure, as below.

### 3.1 Old infrastructure (Phase 1): defects that are not model results (verified unless noted)

- **VS Code settings path.** The checkers read `/home/user/.config/Code/User/settings.json` (task config), but in the Phase-1 guest VS Code runs with `--user-data-dir=/tmp/code-profile` (stated by sonnet5-old in task 276cc624; present in 26 of its transcripts). GUI edits land elsewhere; agents that edit `~/.config/Code/User/settings.json` or `/root/.config/...` do not create the expected file either. Every settings/keybinding task is 0% in every old arm (8 tasks in the never-solved list) while the vscode bucket passes 87.5% (24 runs) for sonnet5-new.
- **Thunderbird profile.** Thunderbird checkers read `/home/user/.thunderbird/t5q2a5hp.default-release/` (profile prepared by the task setup); in the Phase-1 transcripts the running Thunderbird uses `/root/.thunderbird/<other profile>/` and the agents report "No Email Account Configured". Account-dependent tasks are unachievable (sonnet5-old declared FAIL citing a missing precondition in 21 runs, 20 of them Thunderbird tasks) and preference tasks write to the wrong profile (agents claim success, checker 0). The Thunderbird bucket passes 19.0% in both old arms and 76.9% (26 runs) in sonnet5-new; 13 Thunderbird tasks are in the never-solved list.
- **`run_python` runs in the controller, not in the user's shell.** The agent's own output: `cwd: /app/desktop_env/server`; files saved to `/root/Desktop/...`. Tasks whose checker works on "the current directory" or `/home/user` fail although the agent reports success (5c1075ca, 6f56bf42, aad10cd7). The official protocol has no such tool.
- **Evaluator/setup errors** (section 2.4) remove 152 + 76 runs of sonnet46-old from the denominator. Chrome's checkers go through the Chrome DevTools protocol; Phase 1 shows CDP connection errors and the new infrastructure added a loopback CDP forwarder (`env/cdp_forwarder.py`). The Chrome bucket passes 22.9% (sonnet46-old) vs 64-68% in the new arms; 11 Chrome tasks flip from 0 to ≥66%. That the CDP fix is the cause is **suspected**, not verified per task.
- **Black desktop for Astra.** Of astra-old's 95 FAILURE verdicts, 51 are give-ups on a black desktop (the agent says "desktop remains black with no accessible controls"), 17 are API-aborted runs, 1 a missing precondition; only 26 (27%) show no environment factor. The Phase-1 Astra numbers are not a statement about the model.

### 3.2 New infrastructure (official protocol)

- **The guest desktop is a bare Openbox session** (black background, no GNOME shell, panel or Settings): screenshots of both models and Sonnet's own description ("a minimal Openbox window-manager session"). It also shows in Phase-1 Sonnet passes (last screenshot 100% black). It is a **handicap, not an impossibility**: tasks that check `gsettings` were solved 3/3 by sonnet5-old with `run_python` (`gsettings set ...`; a4d98375, f9be0997, 94d95f96; 28cc3b7e 2/3). In the official protocol Astra opened a terminal but typed `gnome-control-center` and declared [INFEASIBLE]; none of its 30 runs on these six tasks ever used `gsettings` (grep). So the `os` bucket (Astra new 26.1%) mixes a real environment handicap with agent behaviour (premature give-up).
- **DISPLAY mismatch.** The guest image sets `DISPLAY=:99` (Dockerfile) while the vendored official prompt tells agents "you MUST use DISPLAY=:0". Terminal launches that follow the prompt fail with "cannot open display" (screenshots; Sonnet 5 new reports it fixed it itself in 2fe4b718 and spent its step budget). Fidelity defect of the Daytona guest, verified by file + transcripts.
- **Give-up behaviour.** Declaring FAIL on feasible tasks: Astra new 11.8% of feasible-task runs (81/685), Sonnet 5 new 1.7% (21/1,214), astra-old 58.8%. Astra is near-perfect on infeasible tasks (63/64 declared) while Sonnet 5 misses 19 of 74.
- **Harness rule penalises time lookups.** 22 astra-new runs (10.2% of its failures) are FAILURE only because the agent called Codex's clock tool (`exec.clock__curr_time`, 20 runs) or `exec.<dynamic>` (2) on tasks with relative dates ("tomorrow", "next weekend", "5th next month"); their end states are not scored.
- **Live sites block the datacenter IP.** Macy's, Kohl's, NBA Store, Drugs.com refuse access (23 Astra, 6 Sonnet failures; screenshots of all runs). Tasks 2888b4e6, 9f3f70fc, b070486d, cabb3bae and the flight/hotel scrapes (fc6d8143, b7895e80, 6c4c23a1) are unsolved by both models.
- **Step cap.** 12.6% of Sonnet 5's failures hit the 100-step cap (49 runs; final messages say "ran out of steps"); Astra batches actions and never does (median 7 calls vs 21).
- **Checkers that reject correct or equivalent output.** `DATEDIF(…,"Y")` vs `"y"` (4e6fcf72, 0/16 across arms; 28 cells differ only in case); Impress PNG export 1792×1008 vs gold 3136×1764 (455d3c66; suspected display-DPI difference); PDF export 214,951 B vs 300,158 B (4bcb1253) and 6,285,079 vs 6,285,092 B (e1e75309).
- **Genuine agent errors exist and are identifiable** from diffs: pivot tables with a different layout (30e3e107), "hide N/A" done with conditional formatting instead of hiding rows (6054afcb), charts claimed but absent (3a7c8185, 347ef137), SSH account whose home directory is not writable (5812b315), magnifier not enabled (3ce045a0), clone placed in a subfolder (acb0f96b), a text answer to a state-graded question (59155008; both models, 0 actions), a refusal to rip a copyrighted song (8f080098, Sonnet).

### 3.3 Tasks nobody solves

73 tasks are never solved by any arm. Each has a hand-written note (task pages, `manual_notes.json`); grouped by the cause the evidence points to: old-infrastructure defects (Thunderbird profile, VS Code profile path, `run_python` cwd/home) (25 tasks); live-site blocking, brittle live-site checkers or the clock rule (6); desktop handicap or suspected infrastructure persistence/fidelity (8); checker strictness or under-specified instructions (8); agent errors/limits/refusals (16); unverified (10). A task can be unsolved for several overlapping reasons; this is the dominant one found.

### 3.4 Old → new: tasks that flip (≥3 informative runs on each side)

7 tasks pass in the old arms (≥66%) and score 0 in the new ones; 37 do the opposite (11 Chrome, 9 Impress, 7 os, 3 Calc, 3 Writer, 2 GIMP, 1 vlc, 1 vscode; full lists in `SUMMARY.md`).

Regressions (verified difference, suspected mechanism):

- **GIMP configuration tasks** (7767eef2 theme, 7b7617bd undo levels, b148e375 layer name): old 6/6, 9/9, 6/6 (the Sonnet 4.6 passes have lost transcripts; Sonnet 5 and, for 7b7617bd, Astra old have readable ones); new 0/10 each, for **both** models. The agents' GUI sequences are identical: sonnet5-old, task 7767eef2, performs Edit > Preferences > Theme > Light > OK (7 actions) and passes 3/3; sonnet5-new and astra-new perform the same sequence and fail 0/10; same for 7b7617bd. Hence the agents are not at fault. The checker reads `gimprc` after a `ctrl+q` postconfig; either the official postconfig does not take effect in this guest or the config lands elsewhere. Same family: d52d6308 (dock, `sessionrc`) and a746add2 (vignette, `action-history`), 0 passes in the new arms.
- **Writer save** (3ef2b351 centre-align heading, b21acd93 line spacing): old 6/7 and 6/6; new 0/5 and 0/4. The old agents press `ctrl+s` themselves (7 actions); the new ones do 3 actions and rely on the official postconfig (`activate_window` + `ctrl+s`), which does not produce the saved file in this guest (suspected: the "Keep current format" prompt is never confirmed, or the hotkey does not reach the X server).
- a4d98375 (lock screen) and 69acbb55 (project environment): solved in Phase 1 through `run_python`; the GUI-only protocol loses them.

Gains: tasks the old arms never solved (0 passes) and the new arms solve ≥66%. Their causes were not examined one by one; for the 11 Chrome tasks the suspected one is the CDP fix (3.1), for others the removal of the `run_python`/HOME/profile defects. 

## 4. Time, steps, tokens, cost (informative runs; medians)

| arm | wall s | agent s | calls | total input tokens | output tokens | cost per run | cost per pass |
|---|---|---|---|---|---|---|---|
| sonnet46-old | 58 | 55 | 13 | 508,084 | 2,448 | $0.22 | $1.17 |
| sonnet5-old | 61 | 57 | 14 | 637,168 | 2,413 | $0.24 | $0.60 |
| astra-old | 49 | — | 9 | 178,290 | 440 | $0.42 (est.) | $1.10 |
| sonnet5-new | 212 | 128 | 21 | 806,068 | 6,709 | $0.49 | $1.65 |
| astra-new | 132 | — | 7 | 109,151 | 981 | $0.38 (est.) | $0.71 |

Sonnet 5 new: passes take 194 s wall and $0.42 per run, failures 350 s and $1.09 (long failing runs hit the cap). Totals over informative runs: Sonnet 5 new $1,514 (CLI cost), Astra new $378 (estimated at the recorded list price), so cost per pass is $1.65 vs $0.71, with the caveat that the two costs are of a different kind. New-infrastructure agents work 3-4× longer per run than in Phase 1 (1080p screenshots, 100-step budget, single-tool surface).

Run-to-run consistency (tasks with ≥2 valid runs): sonnet5-new 149 all-pass / 57 mixed / 54 all-fail (22% flaky); astra-new 93 / 27 / 33 (18% flaky).

## 5. What this does and does not support

- Supported (verified): on the new infrastructure the two models are equal within noise on common tasks; the old infrastructure had defects that explain a large part of its lower scores (VS Code profile path, Thunderbird profile, `run_python` cwd, evaluator errors, black desktop for Astra); the old campaigns' recorded verdicts contain runs that are not agent verdicts; the new infrastructure also regressed on some tasks (GIMP configuration, Writer save) that the same agent procedure passes in Phase 1.
- Supported with caveats: Astra's higher give-up rate on feasible tasks and its failure to fall back to a terminal on a GUI-less desktop; Sonnet 5's step-cap exhaustion on long tasks.
- Not supported: any claim about replicating the published OSWorld-Verified numbers (coverage 43-72% of tasks, environment handicaps, the 361×5 protocol incomplete); a model ranking from the old infrastructure; the causes of the 129 transcript-lost failures; the mechanisms of the GIMP/Writer regressions and of the Chrome gains until reproduced in the VM.

## 6. Pointers

`SUMMARY.md` (all tables), `tables/` (csv: runs, task matrix, timing, by bucket, failures by checker), `tasks/<bucket>/<id>.md` (one cross-arm page per task with per-run evidence; in the external archive, not in git), `METHODOLOGY.md` (definitions, label verification, limits), `manual_notes.json` (95 hand-written task notes), `campaign_analysis.py` (regenerates everything).
