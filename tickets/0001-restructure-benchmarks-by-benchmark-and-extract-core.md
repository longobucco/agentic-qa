# 0001 — Restructure `benchmarks/` by benchmark + extract shared `core/`

- **Status:** Phase 0 + Phase 1 shipped (refactor + hardened `core/`); Phase 2 (BrowseComp)
  and Phase 3 (WebArena) remain as follow-ups — they need external data/infra to land.
- **Created:** 2026-06-24
- **Type:** Refactor
- **Scope:** `benchmarks/`, top-level `data/`
- **Target benchmarks:** WebVoyager (exists), **BrowseComp**, **WebArena** — the last two are
  the ones we run for **harness, not model**, comparison.

## Problem

The repo conflates two different axes into one directory level:

1. **Benchmark** — the task set + judge (WebVoyager, BrowseComp, WebArena, …)
2. **System under test** — the agent being evaluated (our agent-browser + Claude Code; Alumnium; …)

`benchmarks/alumnium/` is the symptom. Alumnium is **not a benchmark** — it's a system
that was run *against* the WebVoyager benchmark. The evidence:

- `benchmarks/alumnium/README.md` is titled "Re-running the Alumnium WebVoyager pipeline."
- Everything in it reaches into `../webvoyager`: calls `../webvoyager/download_data.py`,
  judges with `../webvoyager/evaluate.py`, writes to `../webvoyager/results/alumnium`.
- The actual runner, `alumnium_run.py`, already lives in `benchmarks/webvoyager/`, not in
  the alumnium dir.

So `benchmarks/alumnium/` is really three loose files (`setup.sh`, `extract_failures.py`,
README) describing a WebVoyager run with a different agent.

This won't scale, and **BrowseComp + WebArena make it worse than just "more rows."** They
don't share WebVoyager's shape: BrowseComp judges a short text answer against a reference
(no screenshots), and WebArena judges **deterministically** against a **self-hosted site
stack** with no LLM at all. The current flat layout has WebVoyager's LLM-screenshot judge,
live-site assumptions, and `site`-keyed reporting baked straight into the modules — adding
two benchmarks with different judges and a different environment model would mean three
copies of subtly-incompatible infra.

## Goal

- One directory per **benchmark** (the user's instinct — correct for axis #1).
- "Which agent" modeled as a **runner/adapter within** a benchmark, not a top-level dir.
- Shared machinery extracted to `core/` so a new benchmark is just a new folder with
  tasks + a runner, with no copy-pasted infra.
- **Validate the extraction against two deliberately different benchmarks.** WebVoyager
  alone can't prove `core/` generalizes — it would just bless a WebVoyager-shaped `core/`.
  BrowseComp (live web, LLM answer-grader, calibration metric) and WebArena (self-hosted
  stack, deterministic functional eval, state reset) pull the abstractions in opposite
  directions. If `core/` serves all three, it's real.

## Why these two stress the design (and that's the point)

| Axis | WebVoyager | BrowseComp | WebArena |
|---|---|---|---|
| Task source | download `jsonl` | download **+ decrypt** canary-encrypted Q/A | vendored task configs **+ provision a site stack** |
| Environment | live sites → just a browser | live sites + web search/fetch | **self-hosted Docker stack** + **per-task state reset** |
| Actuation | agent-browser (DOM tree) | agent-browser + search/fetch (read-only research) | agent-browser against the hosted stack |
| Judge | LLM **screenshot** judge (nondeterministic, over-credits) | LLM **answer-vs-reference** grader (no screenshots) | **deterministic** string / url / DOM check — **no LLM** |
| Metric | success rate, per-site | accuracy **+ calibration** (confidence) | success rate, per-site |
| Runs / variance | nondeterministic (live web, temp>0) → **N runs/task + mean±std**; judge itself nondeterministic → repeat judge too | nondeterministic → **N runs/task**; calibration aggregated across runs | **deterministic** → 1 run, no variance |
| Headline caveat | judge over-credits; site drift; single-run scores statistically thin | contamination / eval-awareness; encrypted set | heavy infra; results corrupt without state reset |

Three things fall out that WebVoyager alone hides:

1. **The judge is not one function — it's a pluggable strategy.** Screenshot-LLM,
   answer-grader-LLM, and deterministic-functional are three different implementations
   behind one `judge(task, answer, trajectory) -> Verdict` seam. WebArena's judge uses
   **none** of the `claude -p` plumbing.
2. **"Set up the world for a task" is a real lifecycle**, not an assumption. It's a no-op
   for live-site benchmarks and a stack-bring-up + state-reset for WebArena. There is no
   such concept in the code today.
3. **The LLM judge is a necessity, not a preference — and it makes runs-per-task a `core/`
   concern.** WebVoyager/BrowseComp judge open-ended tasks on the *live* web, where there
   is no deterministic oracle: we don't own the backend, many different answers/paths are
   valid, and the agent's output is free-form. Only a model can decide "did this satisfy
   the intent" from the answer + last *k* screenshots (~85% agreement with humans, so ~15%
   it doesn't; it also over-credits a confident `Answer`). WebArena can be deterministic
   *precisely because* it owns a self-hosted stack. The consequence for the refactor: on the
   two LLM-judged benchmarks **both the agent and the judge are nondeterministic**, so a
   single run per task is statistically thin — `core/` must treat **N-runs-per-task +
   mean±std** as first-class, and keep agent variance (re-rolls) separate from judge
   variance (re-judging one trajectory). The original WebVoyager paper ran each task **once**
   (temp 1, ≤15 steps) and only repeated the *judge* 3× for mean/std — we should not inherit
   that single-rollout limitation.

Adding these **now** is what de-risks the `core/` boundary; deferring them risks
extracting a `core/` that only fits WebVoyager.

## Proposed structure

```
core/
  browser.py       # was webvoyager/chrome.py — CDP lifecycle, throwaway profiles
  agent_loop.py    # the claude -p ↔ agent-browser drive loop (core of agent.py)
  judge.py         # claude -p judge PLUMBING (invoke, parse JSON verdict) — LLM judges only;
                   #      Judge seam carries an `is_deterministic` flag (repeat vs run-once)
  environment.py   # NEW: per-task world setup/teardown. No-op for live sites;
                   #      stack bring-up + state reset for WebArena.
  results.py       # results dir IO, resume (eval.json), failure extraction
  reporting.py     # generic pass-rate + per-bucket breakdown + mean±std across runs
  tasks.py         # generic load / filter / subset / interleave (by id | bucket | limit)
  run.py           # shared orchestration loop: filter → provision → run×N → judge → aggregate → report
benchmarks/
  webvoyager/
    data/                  # tasks + reference answers (download_data.py)
    runners/
      agent_browser.py     # our agent (was agent.py)
      alumnium.py          # was alumnium_run.py
    evaluate.py            # LLM screenshot judge (built on core/judge.py)
    config.py
    report.py
    extract_failures.py    # moves here — parses webvoyager results
    repro-alumnium/        # Option B: verbatim vendored reproduction (setup.sh)
    README.md
  browsecomp/
    data/
      download_data.py     # fetch + DECRYPT the canary-encrypted question/answer set
    runners/
      agent_browser.py     # claude -p + agent-browser + WebSearch/WebFetch; read-only
    evaluate.py            # answer-vs-reference grader on core/judge.py (no screenshots);
                           #   also records the agent's stated confidence
    config.py
    report.py              # accuracy + calibration error
    README.md
  webarena/
    env/                   # provision the self-hosted stack (shopping, shopping_admin,
                           #   gitlab, reddit, wikipedia, map, + homepage); URL/auth
                           #   templating; per-task state reset (DB snapshot restore)
    data/                  # vendored WebArena task configs (intent, start_url, eval spec)
    runners/
      agent_browser.py     # agent-browser against the hosted stack
    evaluate.py            # DETERMINISTIC functional checker
                           #   (string_match | url_match | program_html) — uses NO LLM
    config.py              # base URLs, credentials
    report.py
    README.md
assets/                    # was top-level data/ (demo media, not benchmark data)
```

Split principle: **`core/` = how you run/score/host anything; `benchmarks/<name>/` = the
tasks, prompts, judge, and environment that make it that benchmark.**

## How it reshapes `core/`

- **`judge.py` shrinks to plumbing.** It keeps the `claude -p` invoke + JSON-verdict parse
  (today's `evaluate.py` internals) and exposes a `Judge` seam. WebVoyager's screenshot
  judge and BrowseComp's answer-grader are two implementations on top of it; WebArena's
  functional checker is a third that imports none of it. This is the cleanest proof the
  judge is decoupled. The seam carries an `is_deterministic` flag so `run.py` knows whether
  to **repeat the judge** for variance (LLM judges, which disagree run-to-run) or call it
  **exactly once** (WebArena's functional checker).
- **`environment.py` is new** and is the main thing BrowseComp/WebArena add over the
  original plan. A context manager `with benchmark.environment(task) as env:` that yields
  whatever the runner needs (a browser endpoint; for WebArena, also the reset hooks). The
  live-site impl is a thin wrapper over `core/browser.py`; WebArena's impl owns the stack.
- **`tasks.py` + `reporting.py` generalize `site` → `bucket`.** WebVoyager buckets by
  `web_name`, BrowseComp by topic/category, WebArena by site. Same stratified-subset /
  per-bucket-breakdown code, just not hardcoded to `site`. `reporting.py` also emits
  **mean±std across runs** and surfaces **agent variance vs judge variance** separately, so
  a harness-A/B delta can be called significant (or not) instead of read off one run.
- **`run.py` becomes the shared loop.** Today `run_all.py` already does filter → resume →
  concurrency → judge → report; lift it to `core/run.py` so each benchmark's entry point is
  thin and the `--concurrency`/resume semantics live in one place. It also owns
  **runs-per-task** (`--runs N`): re-roll each task, aggregate verdicts, and for LLM judges
  re-judge per the `is_deterministic` flag — single-run scores don't survive the harness
  comparisons in Phase 2/3.

## Tasks

### Phase 0 — fold Alumnium into WebVoyager, extract `core/` ✅
- [x] `git mv benchmarks/webvoyager/chrome.py core/browser.py` (+ judge/results/reporting
      extraction from existing webvoyager modules)
- [x] Move `alumnium_run.py` → `benchmarks/webvoyager/runners/alumnium.py`; `agent.py` →
      `runners/agent_browser.py`
- [x] Replace `benchmarks/alumnium/` with a `--system {agent_browser,alumnium}` flag on the
      WebVoyager runner; move `extract_failures.py` into `benchmarks/webvoyager/`
- [x] Relocate the verbatim-repro `setup.sh` to `benchmarks/webvoyager/repro-alumnium/`,
      clearly labeled "their harness, for exact reproduction"
- [x] Fix all relative paths (`../webvoyager/...`) broken by the moves
- [~] Rename top-level `data/` → `assets/` — **N/A**: there is no top-level `data/` (it was
      removed in commit 9803aa3 before this refactor); nothing to rename.
- [x] Update READMEs to reflect the new layout
- [x] Smoke test: `run --system alumnium --limit 1` and the agent-browser path — verified
      via `--dry-run` (resolved commands + arg parsing + task/ref loading). A *live* run
      additionally needs Chrome-for-Testing + a `claude` login (+ OPENAI key for alumnium).

### Phase 1 — harden `core/` for more than one benchmark ✅
- [x] Define the `Judge` seam in `core/judge.py`; reduce it to `claude -p` plumbing +
      verdict parsing. Re-express `benchmarks/webvoyager/evaluate.py` as the screenshot
      judge built on it (same verdict logic as the original `evaluate.py`).
- [x] Add `core/environment.py`: a per-task setup/teardown context manager. Ship the
      no-op (`null_environment`) + live-site (`live_site_environment`) impls and port
      WebVoyager onto them (agent_browser uses live-site; alumnium brings its own browser).
- [x] Generalize `core/tasks.py` + `core/reporting.py` from `site` → a `bucket` key.
- [x] Lift `run_all.py`'s loop to `core/run.py`; WebVoyager's entry point calls it.
- [x] Make **runs-per-task** first-class: `--runs N` (default 1) in `core/run.py` +
      **mean±std** aggregation in `core/reporting.py`, reporting agent variance and judge
      variance separately. Added the `is_deterministic` flag to the `Judge` seam (LLM judges
      repeat via `--judge-repeats`; a deterministic checker runs once).

### Phase 2 — BrowseComp (live web, read-only research)
- [ ] `data/download_data.py`: fetch + **decrypt** the canary-encrypted question/answer set.
- [ ] `runners/agent_browser.py`: `claude -p` + agent-browser + WebSearch/WebFetch,
      read-only (no form submission — unlike ClawBench there's no state-change to intercept).
- [ ] `evaluate.py`: answer-vs-reference grader on `core/judge.py` (text only, no
      screenshots); capture the agent's stated confidence for calibration.
- [ ] `report.py`: accuracy + calibration error.
- [ ] README + 1-task smoke (`--limit 1`).
- [ ] **Harness-A/B protocol:** pin `WV_MODEL`, hold attempt budget + tool policy fixed
      across runner variants so score deltas reflect the harness, not the model. Run each
      variant over `--runs N` and only call a delta real once it clears the mean±std from
      `core/reporting.py` — a single-run gap is inside the noise floor.

### Phase 3 — WebArena (self-hosted, deterministic)
- [ ] `env/`: provision the site stack (shopping, shopping_admin, gitlab, reddit, wikipedia,
      map, + homepage); URL + auth/cookie templating to your instances; **per-task state
      reset** (DB snapshot restore) — without it, tasks contaminate each other.
- [ ] `data/`: vendor the WebArena task configs (`intent`, `start_url`, eval spec).
- [ ] `runners/agent_browser.py`: agent-browser against the hosted stack.
- [ ] `evaluate.py`: deterministic functional checker (`string_match` | `url_match` |
      `program_html`) — imports nothing from `core/judge.py`. This is the test that the
      judge seam is real.
- [ ] `report.py`; README documents stack bring-up + reset; smoke on one reddit/shopping task.
- [ ] Decide the concurrency story: parallelism needs isolated stacks (one per worker), so
      default serial unless the env is replicated. Document it like the agent-browser
      single-daemon caveat already in WebVoyager.

## Notes

- Use `git mv` to preserve history.
- Staging: (0) fold Alumnium into WebVoyager → (1) extract + harden `core/` →
  (2) BrowseComp → (3) WebArena. Land Phase 1 before Phase 2/3 so the new benchmarks build
  on the hardened seams instead of forking infra.
- BrowseComp and WebArena are the two we actually compare harnesses on; their value is in
  the **same-run deltas** between harness variants, not absolute scores — so wire the
  pinned-model / fixed-budget protocol into both `report.py`s from the start.
- Benchmark-specific caveats to carry into each README:
  - **WebVoyager:** the LLM screenshot judge is nondeterministic and over-credits — it sees
    only the last *k* screenshots and agrees with humans ~85% of the time, so report each
    task over **N runs as mean±std**, never a single number. The benchmark is observation-
    agnostic (tasks are just intent + start URL); the screenshot+Set-of-Mark observation is
    *our runner's* choice, not the benchmark's — a DOM/accessibility-tree runner scores on
    the same tasks. Time-sensitive tasks (Booking, Google Flights) carry hardcoded dates —
    **refresh them on download** or they fail as stale, not as agent errors.
  - **BrowseComp:** dataset is canary-encrypted (decrypt on download); strong contamination
    / eval-awareness risk — treat same-day A/B deltas as the signal, not the leaderboard
    number.
  - **WebArena:** heaviest infra of the three; deterministic eval is the payoff but only if
    state is reset between tasks and task-config URLs are templated to your hosted stack.
