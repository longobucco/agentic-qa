# Methodology

How the numbers in `SUMMARY.md`, `FINDINGS.md` and `tables/` were produced, and how far they can be trusted.

## 1. Data

Source: the raw OSWorld result trees archived on 2026-10-14 (byte-for-byte verified copies of the local result directories, which git does not track). Layout of a run: `results/<campaign>/<task_id>/run_<k>/` with `result.json` (agent record, provenance, tokens, cost), `eval.json` (official verdict), `infra_error.json` (every aborted attempt before this one), `agent_output.txt`, `conversation.jsonl` (full transcript, screenshots included), `final.png` (Claude arms and the new Sonnet arm), `eval_artifacts/` (the agent's output file and the gold, when kept).

Only `run_<integer>` directories are counted (archived `run_*_legacy` and `_probes` are excluded), which is why the counts differ from earlier ad-hoc counts that included them.

### Arms

An *arm* groups campaigns that share model and harness. Headline numbers use only the *main* campaign of each arm; canaries, smoke tests, probes and re-checks are analysed in the per-task pages but not in the headline.

| arm | model / runtime | harness | main campaign (tree) |
|---|---|---|---|
| sonnet46-old | Claude Sonnet 4.6 (+ Haiku helper), Claude Code CLI | Phase 1: many MCP tools (screenshot, click, key, type, `run_python`, `a11y_tree`), 150 turns, 2,700 s | `agent_computer` |
| sonnet5-old | Claude Sonnet 5, Claude Code CLI | Phase 1 (same tools), model pinned | `agent_computer_sonnet5` |
| astra-old | gpt-6-astra, Codex CLI | Phase 1 tool surface, closed-book, 30 advisory steps | `agent_computer_astra` |
| astra-old-openbook | gpt-6-astra, Codex CLI | Phase 1, open-book arm (different protocol, reported separately, small) | `agent_computer_astra_openbook` |
| sonnet5-new | Claude Sonnet 5 (effort max) | official protocol: single `computer` tool, 1080p, 100 steps, official evaluators | `..._sonnet5_effortmax_protocol361img3_official` |
| astra-new | gpt-6-astra (reasoning max), Codex CLI 0.153.4 | official protocol, same tool, 100 steps | `..._gpt6astra_max_codex01534_protocol361img3_official` |

## 2. Definitions

- **Recorded verdict**: `eval.json.verdict`. `SUCCESS`/`FAILURE` = a score was produced by the official evaluator; `EVAL_ERROR` = the evaluator errored (offline fallback); `ENVIRONMENT_ERROR` = task setup failed; no `eval.json` = no verdict.
- **Informative run**: a recorded SUCCESS/FAILURE minus (a) runs whose agent CLI aborted (session limit, not logged in, API error, quota) although a verdict was written, and (b) *no-op passes*: SUCCESS with 0 tool calls on a task that is not infeasible. Headline pass rates are over informative runs; the recorded rate is always shown next to it.
- **Transcript lost**: during an August/September transcript back-fill, some runs were re-executed, hit the quota, and the transcript was overwritten with the single message "You've hit your session limit". Their `eval.json` and `final.png` predate that and are genuine (checked by timestamps and by viewing a final screenshot). They count as normal verdicts but their failure cause cannot be read from the transcript (283 runs in sonnet46-old, 8 in sonnet5-old).
- **Pass rate** is per run (a run = one trajectory); the cross-arm comparison uses a per-task score (mean over the arm's informative runs) averaged over the tasks both arms have a verdict for, so different numbers of runs per task do not bias it.
- **Steps**: number of tool calls to the OSWorld MCP server in the transcript. Astra batches several actions per call, so Astra step counts are not comparable with Sonnet's (median 7 vs 21 in the new infrastructure).
- **Time**: `wall` = provenance `finished_at - started_at` of the recorded attempt; `agent` = Claude `duration_ms` (not available for Codex). Medians are reported; means and p90 are in `tables/timing_by_arm.csv`.
- **Tokens / cost**: Claude arms: fresh + cache-read + cache-write input tokens; `cost_usd` is the CLI's own figure. Codex arms: `input_tokens` already includes cached tokens; cost is an estimate at the list price recorded in the run ("2026-09-09 list price: $10/M input, $1/M cached, $50/M output"). Costs of the two families are therefore not strictly comparable.

## 3. Failure taxonomy

Every non-pass run gets a **mode** (what the agent or the harness did) and, optionally, an **environment factor** (a condition that plausibly blocked it). They are kept separate on purpose: an environment factor is never reported as *the* cause.

Modes (FAILURE verdicts): `claimed-done-wrong` (finished with an explicit DONE but the checker scored 0; the residual class and by far the largest), `declared-fail` (answer FAIL / `[INFEASIBLE]` on a task the checker expects done), `declined-without-acting` (0 tool calls and FAIL: refusal or confirmation request), `answered-in-text-no-action` (0 tool calls, text answer), `capped` (CLI stopped on max turns/timeout, or the run says it ran out of steps), `tool-surface-violation` (harness rule, astra-new only), `checker-strict-case-only` (output equals the gold except letter case), `contaminated` (harness flag), `transcript-lost`, plus the non-agent classes `cli-error`, `api-error`.

Factors (only on runs that gave up or hit the cap): `black-desktop` (the guest is a bare black Openbox session without GNOME shell/panel/Settings and the agent says it cannot proceed), `site-blocked` (live site refused the agent: Access Denied, bot detection), `missing-precondition` (the agent reports a missing file/app/account the task assumes), `tool-http-error` (≥3 desktop-tool calls returned HTTP/connection errors).

Not agent verdicts: `INFRA-CLI` (CLI aborted: session limit, weekly limit, not logged in, API error), `INFRA-API` (agent run killed by an API error such as 429 or "model at capacity" after a partial transcript), `ORACLE-ERROR` (EVAL_ERROR), `INFRA-ERROR` (ENVIRONMENT_ERROR), `INCOMPLETE` (no verdict: quota, infra flake, interrupted).

The labels are produced by rules over the verdict, the checker name, the final message, the stop reason, the transcript errors, the last screenshot (fraction of black pixels) and, for file tasks, a diff of the agent's output against the gold (xlsx cell by cell incl. hidden rows/columns and charts, pptx slide text and backgrounds, text and image sizes).

## 4. Verification of the labels

The rules were checked against transcripts. For each (arm, cause) cell a deterministic pseudo-random sample was read (final message, last actions, errors, screenshots or diffs) and judged *correct / partial / wrong*. Two rounds were done; the tables and the defects found are in the last section of `SUMMARY.md`. Round 1 read 77 runs and found the labels `capped` and `action-loop` unreliable (the old Phase-1 30-step cap is advisory; the repeated-action signature truncated arguments) and the environment factors incomplete (missing file/app/account, black desktop reported by the agent). Round 2 (17 more runs, on the corrected rules) found factors attached to runs that actually finished (agents mention the black start screen or a transient `run_python` HTTP 500 and then complete the task); factors are now attributed only to runs that gave up or hit the cap.

What was verified by hand beyond the sample: 95 tasks have a hand-written note (`manual_notes.json`, shown at the top of each task page and in `tables/task_matrix.csv`) with a confidence level; the claims marked *high* rest on a checker definition, a diff, a screenshot or an explicit statement in several transcripts. Claims marked *low* or *UNVERIFIED* are hypotheses.

## 5. Limits

- The dominant class, `claimed-done-wrong`, says what happened (the agent declared success and the checker scored 0), not why. For file tasks the diff narrows it; for state tasks (settings, configs) the cause is often unknowable without re-running in the VM.
- A significant part of the old infrastructure's failures are environment defects (see `FINDINGS.md`), so old-vs-new differences mix model and infrastructure changes. They are not a clean model comparison.
- Rates are over runs that produced a verdict: the new campaigns cover only 264 (Sonnet 5) and 156 (Astra) of the 361 tasks; the old ones cover a different task subset each. The fair-comparison table restricts to common tasks.
- `astra-old-openbook` is small after excluding rate-limited runs.
- Timing medians include the attempts' own queueing; a few attempts waited for quota (wall times of hours) and move means, not medians.
- No run was re-executed. Hypotheses about the guest (GIMP persistence on exit, LibreOffice config flush, DPI of image exports) are not verified in the VM.

## 6. Reproduce

```
python campaign_analysis.py <archive_root> <out_dir> manual_notes.json verification.json
```
needs Python 3.11 with Pillow, openpyxl and python-pptx; ~20 minutes for ~4,800 runs (the new Sonnet campaign alone is ~19 GB of transcripts).
