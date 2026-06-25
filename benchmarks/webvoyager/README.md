# WebVoyager harness — agent-browser + Claude Code

Runs the [WebVoyager](https://arxiv.org/abs/2401.13919) benchmark with a minimal agent:
**`agent-browser`** (Vercel, DOM/accessibility-tree actuation) as the hands, and
**Claude Code (`claude -p`)** as the brain *and* the judge — so the whole pipeline runs on
your Claude subscription with **no API keys**. A second runner re-runs the same tasks
through the **Alumnium** pipeline for comparison.

Everything reusable lives in the repo-root **`core/`** package (browser, drive loop, judge
seam, environment, results, reporting, tasks, run loop). This directory holds only what
makes WebVoyager *WebVoyager*: its tasks, prompts, screenshot judge, and runners.

```
benchmarks/webvoyager/
  data/                 tasks + reference answers (download_data.py)
  runners/
    agent_browser.py    our agent: claude -p drives agent-browser over CDP
    alumnium.py         Option A: re-run via the Alumnium MCP, OUR judge -> comparable
                        results, same layout as agent_browser (needs OPENAI_API_KEY)
  evaluate.py           WebVoyager screenshot judge (a core.judge.Judge)
  benchmark.py          wires tasks + runners + judge + buckets into a core.Benchmark
  config.py             env-tunable settings + Chrome-for-Testing discovery
  prompts.py            agent + judge prompt templates
  tasks.py              load tasks/refs; bucket = web_name
  run.py                thin entry point -> core.run
  report.py             thin entry point -> core.reporting
  extract_failures.py   list the tasks a run failed (handles run_N/ and flat layouts)
  alumnium/             Option B: verbatim vendored reproduction — THEIR harness +
                        THEIR GPT-5 judge (setup.sh). NOT comparable to our results.
  docker/               Steel + agent-browser image (parked: per-worker daemon isolation)
```

## Prereqs (one-time)
```bash
npm install -g agent-browser && agent-browser install   # downloads Chrome for Testing
claude --version                                         # logged in to your subscription
```

## Where do the browsers run?
**Locally**, as a Chrome-for-Testing process the harness launches and controls over CDP
(`core/browser.py`, provisioned per task by `core/environment.py`). No Docker/VM required
for the default path. Run it in a **normal terminal** (not a restricted shell) so the
browser has real network to reach live sites.

## Run
All commands are run as modules **from the repo root** (so `core/` is importable):
```bash
python -m benchmarks.webvoyager.download_data          # once: fetch tasks + references
python -m benchmarks.webvoyager.run --ids ArXiv--0     # 1-task smoke (our agent)
python -m benchmarks.webvoyager.run --per-bucket 3     # stratified subset, serial
python -m benchmarks.webvoyager.run                    # full set (resumable)
python -m benchmarks.webvoyager.run --runs 3           # 3 agent re-rolls -> mean±std
python -m benchmarks.webvoyager.report                 # re-print summary anytime
```

`--bucket`/`--site` and `--per-bucket`/`--per-site` are aliases. Config via env:
`WV_MODEL`, `WV_HEADED=1`, `WV_MAX_STEPS`, `WV_TASK_TIMEOUT`, `WV_CHROME_BIN`,
`WV_PORT_BASE`.

## CONCURRENCY — important
`agent-browser` has **one shared daemon**; `connect` only binds the `default` session, so
the `agent_browser` runner is **not concurrency-safe** — keep `--concurrency 1` locally
(>1 cross-contaminates sessions and the loop will warn). True parallelism requires a
separate agent-browser daemon per worker (a container per worker — see `docker/`, parked).
The `alumnium` runner has no such limit (each `claude -p` gets its own Alumnium MCP +
Selenium Chrome), so it parallelizes: `--concurrency 4`.

## Runs-per-task & variance (matters for the paper)
On this benchmark **both the agent and the judge are nondeterministic** (live web, temp>0;
an LLM screenshot judge that agrees with humans ~85% of the time and over-credits a
confident `Answer`). A single run per task is statistically thin, so:
- `--runs N` re-rolls each task N times; `report` prints the pass rate as **mean ± std
  across runs** (agent variance).
- `--judge-repeats M` re-judges each trajectory M times; `report` prints the **mean judge
  disagreement** separately (judge variance). The original WebVoyager paper ran each task
  once and only repeated the judge 3×; don't inherit that single-rollout limitation.

A harness-A/B delta is only real once it clears the spread — don't read it off one run.

## Alumnium comparison
Goal: regenerate Alumnium's WebVoyager run and recover **the ~1.5% of tasks it failed**
(98.5% on 610 tasks ≈ 9 failures), a list published nowhere. Run the pipeline, then
`extract_failures.py`.

Credentials:
- **Orchestrator** — Claude Code (`claude`) on your subscription → no key.
- **Alumnium `do/check` model** — needs a provider key. We use **OpenAI** (their run used
  GPT-5 Nano via Azure; OpenAI is equivalent + one fewer dependency).
- **Judge** — our subscription Claude screenshot judge (`evaluate.py`) → no key.

### Option A — adapted, single key (recommended)
Reuses this harness; the Alumnium MCP is the browser layer, Claude judges. `OPENAI_API_KEY`
auto-loads from the **repo-root `.env`** (shared by all benchmarks) — no manual export needed.
```bash
python -m benchmarks.webvoyager.run --system alumnium --per-bucket 3 --concurrency 4
python -m benchmarks.webvoyager.run --system alumnium --concurrency 4   # full (resumable)
python -m benchmarks.webvoyager.report alumnium
python -m benchmarks.webvoyager.report --compare agent_browser alumnium  # harness A/B + Δ
python -m benchmarks.webvoyager.extract_failures benchmarks/webvoyager/results/alumnium
```

### Option B — exact fidelity (their harness verbatim)
For a literal reproduction (their `run_claude_code.py` + GPT-5 `auto_eval.py`, which also
captures per-step screenshots). Needs OpenAI and matches their Azure-style config.
```bash
benchmarks/webvoyager/alumnium/setup.sh                # clone alumnium + WebVoyager fork
# run their runner/evaluator under alumnium/vendor/... (see setup.sh output), then:
python -m benchmarks.webvoyager.extract_failures \
  benchmarks/webvoyager/alumnium/vendor/alumnium/benchmarks/webvoyager/results/claude-code
```

**Deviations to note in the paper:** Option A swaps Azure→OpenAI for the do/check model and
GPT-5→Claude for the judge, and has no per-step screenshots (Alumnium MCP exposes no
screenshot tool, so the judge scores the answer against the reference). Option B removes
those deviations. `extract_failures.py` tolerates both output formats.

## Caveats (matter for the paper)
- The judge is an LLM → nondeterministic and tends to over-credit (inherent to
  WebVoyager's auto-eval, originally GPT-4V). Report it over **N runs as mean±std**, never
  a single number.
- The benchmark is observation-agnostic (tasks are just intent + start URL); the
  screenshot + Set-of-Mark observation is *our runner's* choice — a DOM/accessibility-tree
  runner scores on the same tasks.
- Live sites drift; time-sensitive Booking/Google-Flights tasks carry hardcoded dates —
  refresh them on download or they fail as stale, not as agent errors.
- Anti-bot IP-blocks happen on a residential IP (e.g., Allrecipes). Use a proxy / cloud
  browser for those; treat blocks as environment failures, not agent failures.
