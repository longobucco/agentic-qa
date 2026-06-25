# WebArena harness — agent-browser + Claude Code, **deterministic** eval

[WebArena](https://webarena.dev) runs tasks against a **self-hosted site stack** (shopping,
shopping_admin, gitlab, reddit, wikipedia, map, homepage) and grades them **deterministically**
— string / url / DOM checks, **no LLM**. It's the third benchmark on the shared `core/`, and
the one that proves the seams generalize:

- **Judge** (`evaluate.py`) is a deterministic functional checker (`is_deterministic=True`) that
  imports **none** of `core.judge`'s `claude -p` plumbing. (WebVoyager = screenshot LLM judge;
  BrowseComp = answer-vs-reference LLM grader; WebArena = this. Three implementations, one seam.)
- **Environment** (`env/stack.py`) is the first non-trivial `core.environment`: **per-task
  state reset** (without it, tasks contaminate the shared stack).

```
benchmarks/webarena/
  data/download_data.py   vendor upstream config_files/test.raw.json -> webarena.jsonl (812 tasks)
  env/stack.py            per-task DB-snapshot reset (WA_RESET_<SITE>) — the environment seam
  env/daytona.py          provision a site as an x86 Daytona sandbox -> public WA_<SITE>_URL
  env/provision.md        the stack bring-up + per-task reset RUNBOOK (empirical Daytona limits)
  runners/agent_browser.py  Steel agent vs the hosted stack; captures final_url + program_html
  runners/colorbrowser.py   comparator: ColorBrowserAgent (BrowserGym, self-evaluating) vs the stack
  evaluate.py             DETERMINISTIC checker (string_match | url_match | program_html)
  tests/test_evaluate.py  unit-proves the judge with NO stack/LLM  (python -m ...tests.test_evaluate)
  config.py / tasks.py / benchmark.py / run.py / report.py
```

## Two pipelines
Both run against the SAME hosted stack on deterministic WebArena eval, so the comparison is
"our pipeline vs a third-party SOTA agent":
- `agent_browser` — **our pipeline**: `claude -p` + agent-browser (DOM/accessibility-tree) in a
  Steel container; scored by our deterministic judge (`evaluate.py`). Claude subscription.
- `colorbrowser` — **ColorBrowserAgent** ([MadeAgents/browser-agent](https://github.com/MadeAgents/browser-agent),
  71.2%, Dec 2025 #1): a different agent *system* (BrowserGym dual-agent Summarizer+Operator, GPT-5).
  Containerized (`benchmarks/webarena/docker/Dockerfile.colorbrowser`), pointed at the stack via `WA_<SITE>` env;
  **self-evaluating** — its own `cum_reward` is the same WebArena eval, so `core.run` uses its
  verdict (the `Runner.self_eval` path), not our judge. Needs `OPENAI_API_KEY` (GPT-5 by default;
  `CBA_MODEL` to change). `--compare agent_browser colorbrowser` shows the delta. Note: this varies
  harness AND model (GPT-5 vs our Claude); it's "our pipeline vs the SOTA agent," not model-pinned.

Build the comparator image once:
`docker build -f benchmarks/webarena/docker/Dockerfile.colorbrowser -t colorbrowser:latest benchmarks/webarena/docker`.

Both **serial** here (shared stack + per-task reset).

## ⚠️ This needs an x86 host — it will NOT run on an arm64 Mac
The official WebArena sites are **multi-GB amd64 `.tar` snapshots** you `docker load`, plus
**Wikipedia ≈180GB** and **Map ≈180GB** of data. That's ~400GB of emulated x86 services with
per-task DB resets — built for a beefy x86 Linux box / cloud VM, not a laptop. **Provision it on
an x86 host** (e.g. a Daytona sandbox) and point this harness at it.

You only need the **sites you choose** — tasks group by site, downloaded per-site, never all at
once. Skip the two 180GB giants and ~709 tasks remain:

| site | footprint | tasks |
|---|---|---|
| reddit (Postmill) | lightest (~few GB) | 106 |
| gitlab | moderate | 204 |
| shopping / shopping_admin | moderate–large | 192 / 184 |
| wikipedia / map | ~180GB each — skip | 23 / 128 |

## Provision (on the x86 host / Daytona sandbox)
The full runbook + the **empirically-measured Daytona limits** are in
[`env/provision.md`](env/provision.md). The short version:

- **Daytona reality (measured):** per-sandbox disk caps at **10GB** and its Volumes are S3/FUSE
  that reject the `chmod` docker's data-root needs — so the ticket's "docker data-root on a
  Volume" split is **dead**. The path that works is **create the sandbox FROM the 45GB image**
  (image = read-only base, the 10GB overlay takes Postgres writes); `env/daytona.py` does this.
  Daytona runs its own init, so the site's services are started by hand after create.
- **Or** any x86 Docker VM with ≥60GB disk: `docker run` the image directly (identical harness,
  only `WA_<SITE>_URL` changes).

```bash
python -m benchmarks.webarena.env.daytona up reddit \
    webarenaimages/postmill-populated-exposed-withimg:latest 80   # -> WA_REDDIT_URL
export WA_REDDIT_URL=<printed public url>
export WA_RESET_REDDIT="python -m benchmarks.webarena.env.daytona reset <sandbox-id> reddit"
docker build -f benchmarks/webvoyager/docker/Dockerfile.steel-ab -t steel-ab:latest \
    benchmarks/webvoyager/docker/                                 # the Steel agent image
python -m benchmarks.webarena.env.daytona down <sandbox-id>       # ALWAYS tear down (costs $)
```

## Run
```bash
python -m benchmarks.webarena.data.download_data            # once: vendor 812 task configs
python -m benchmarks.webarena.run --per-bucket 1            # one task per provisioned site
python -m benchmarks.webarena.run --bucket reddit          # all runnable reddit tasks (agent_browser)
python -m benchmarks.webarena.run --system colorbrowser --bucket reddit  # the SOTA comparator
python -m benchmarks.webarena.report                       # success rate by site
python -m benchmarks.webarena.report --compare agent_browser colorbrowser # our pipeline vs SOTA + Δ
```
`load_tasks` automatically **skips tasks whose sites aren't provisioned**, so a reddit-only
stack runs the 106 reddit tasks and ignores the rest.

## Caveats
- **Serial by default** — one shared stack + per-task reset means concurrency would corrupt
  state. Replicate the stack per worker to parallelize (like WebVoyager's single-daemon caveat).
- **Deterministic eval is the payoff** — but only correct if state is reset between tasks and
  URLs are templated to your stack. `fuzzy_match` tasks (a minority) need an LLM and are out of
  scope for this deterministic checker.
- The deterministic checker is **unit-tested without any stack**: `python -m
  benchmarks.webarena.tests.test_evaluate`.
