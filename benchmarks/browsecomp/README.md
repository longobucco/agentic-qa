# BrowseComp harness — agent-browser + Claude Code

Runs [BrowseComp](https://openai.com/index/browsecomp/) (OpenAI): ~1,266 deliberately-hard
questions answerable only by **researching the live web**. The agent (Claude Code driving
`agent-browser` in a per-task Steel container) searches and cross-checks to produce a short
answer + a stated confidence; an LLM grades the **answer against the reference text**.

This is the second benchmark on the shared `core/`, and it deliberately differs from
WebVoyager in two seams:
- **Judge**: an **answer-vs-reference text grader** (`evaluate.py` on `core.judge`) — *no
  screenshots*. WebVoyager's screenshot judge and this one are two implementations of the
  same `Judge` seam.
- **Task**: there is **no start URL** — the agent decides where to search.

```
benchmarks/browsecomp/
  data/
    download_data.py    fetch OpenAI's CSV + canary-DECRYPT -> browsecomp.jsonl
    browsecomp.jsonl    {id, ques, answer(reference), bucket(topic)}  (gitignored)
  runners/
    agent_browser.py    research via the shared core.steel container (concurrency-safe)
    claude_code.py      A/B comparator: PLAIN Claude Code (native WebSearch/WebFetch, no browser)
  evaluate.py           answer-vs-reference grader (a core.judge.Judge, text only)
  prompts.py            research agent prompt + grader prompt
  tasks.py              load tasks/refs; bucket = problem_topic
  config.py / run.py / report.py
```

## Two pipelines (harness A/B)
BrowseComp is a **research** benchmark — its leaderboard is deep-research agents/models, not
GUI browser frameworks — so the most informative comparator isolates *the browser harness
itself*, holding the model fixed:
- `agent_browser` — `claude -p` + agent-browser driving a real Chromium in a Steel container.
- `claude_code` — **plain Claude Code**: the SAME orchestrator + model, but research via Claude
  Code's native `WebSearch`/`WebFetch` tools and NO browser. agent_browser vs claude_code =
  "does driving a real browser beat plain Claude Code research, at the same model?"
Both are `$0` (Claude subscription, grader too), concurrency-safe, graded by the SAME text judge.

## Prereqs
```bash
# the Steel agent-browser image (shared with WebVoyager), for the agent_browser pipeline:
docker build -f benchmarks/webvoyager/docker/Dockerfile.steel-ab -t steel-ab:latest \
  benchmarks/webvoyager/docker/
claude --version                                          # logged in to your subscription
python -m benchmarks.browsecomp.data.download_data        # once: fetch + decrypt 1,266 tasks
```
`claude_code` needs no image — it's plain Claude Code.

## Run
```bash
python -m benchmarks.browsecomp.run --limit 25 --concurrency 4               # agent_browser subset
python -m benchmarks.browsecomp.run --system claude_code --limit 25         # plain-Claude comparator
python -m benchmarks.browsecomp.run --per-bucket 3 --runs 2                  # stratified, variance
python -m benchmarks.browsecomp.run                                         # full set (resumable)
python -m benchmarks.browsecomp.report                                      # accuracy by topic
python -m benchmarks.browsecomp.report --compare agent_browser claude_code  # harness A/B + Δ
```
`agent_browser` runs one Steel container per task, so `--concurrency N` is safe; `claude_code`
is independent `claude -p` calls. Both bill nothing beyond your Claude subscription.

## Caveats (matter for the paper)
- **Captchas / bot walls.** A headless browser doing open-web research can hit search-engine
  bot walls — the prompt prefers DuckDuckGo's HTML endpoint, but some queries may still be
  blocked; treat those as environment failures, not agent failures.
- **Contamination / eval-awareness.** BrowseComp is canary-encrypted for a reason; models may
  have partial exposure. Treat **same-day A/B deltas between harness variants** as the signal,
  not the absolute leaderboard number.
- **Hard by design.** These questions resist plain search — low absolute accuracy is expected;
  the value is comparing harnesses on the same set.
- **Calibration is a follow-up.** We capture the agent's `confidence` per task; binning it
  against correctness (BrowseComp's calibration metric) is not wired into `report.py` yet.
