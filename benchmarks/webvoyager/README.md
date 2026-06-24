# WebVoyager harness — agent-browser + Claude Code

Runs the [WebVoyager](https://arxiv.org/abs/2401.13919) benchmark with a minimal agent:
**`agent-browser`** (Vercel, DOM/accessibility-tree actuation) as the hands, and
**Claude Code (`claude -p`)** as the brain *and* the judge — so the whole pipeline runs
on your Claude subscription with **no API keys**.

```
download_data.py   fetch tasks + reference answers (Alumnium's 2026 set by default)
chrome.py          launch/stop a local Chrome-for-Testing on a CDP debug port
agent.py           run ONE task: claude -p drives agent-browser to complete it
evaluate.py        WebVoyager-style judge via claude -p (reads screenshots)
run_all.py         loop over tasks (filter / subset / resume / report)
report.py          success rate, per-site breakdown, failed-task list
alumnium_run.py    re-run the same tasks via the Alumnium MCP (needs OPENAI_API_KEY)
docker/            Steel + agent-browser image (parked: per-worker daemon isolation)
```

## Prereqs (one-time)
```bash
npm install -g agent-browser && agent-browser install   # downloads Chrome for Testing
claude --version                                         # logged in to your subscription
```

## Where do the browsers run?
**Locally**, as a Chrome-for-Testing process this harness launches and controls over CDP
(`chrome.py`). No Docker/VM required for the default path. Run it in a **normal terminal**
(not a restricted shell) so the browser has real network to reach live sites.

## CONCURRENCY — important
`agent-browser` has **one shared daemon**; `connect` only binds the `default` session, so
**`run_all.py` must stay at `--concurrency 1`** locally (>1 cross-contaminates sessions).
True parallelism requires a separate agent-browser daemon per worker (a container per
worker — see `docker/`, parked). `alumnium_run.py` has no such limit (each `claude -p`
gets its own Alumnium MCP + Selenium Chrome), so it parallelizes: `--concurrency 4`.

## Run
```bash
cd benchmarks/webvoyager
python download_data.py                       # once
python run_all.py --ids ArXiv--0              # 1-task smoke
python run_all.py --per-site 3                # stratified subset (45 tasks), serial
python run_all.py                             # full set (resumable)
python report.py                              # re-print summary anytime

# Alumnium comparison (parallel; key required):
export OPENAI_API_KEY=sk-...                  # or: set -a; . ./.env; set +a
python alumnium_run.py --per-site 3 --concurrency 4
python report.py alumnium
```

Config via env: `WV_MODEL`, `WV_HEADED=1`, `WV_MAX_STEPS`, `WV_TASK_TIMEOUT`,
`WV_CHROME_BIN`, `WV_PORT_BASE`.

## Caveats (matter for the paper)
- The judge is an LLM → nondeterministic and tends to over-credit (inherent to
  WebVoyager's auto-eval, originally GPT-4V). Report it as such.
- Live sites drift; time-sensitive Booking/Google-Flights tasks need date updates.
- Anti-bot IP-blocks happen on a residential IP (e.g., Allrecipes). Use a proxy / cloud
  browser for those; treat blocks as environment failures, not agent failures.
