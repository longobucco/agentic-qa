# Re-running the Alumnium WebVoyager pipeline

Goal: regenerate Alumnium's WebVoyager run and recover **the ~1.5% of tasks it failed**
(98.5% on 610 tasks ≈ 9 failures) — a list published nowhere. You run the pipeline, then
`extract_failures.py` reads each task's `eval.json`.

## Credentials
- **Orchestrator** — Claude Code (`claude`) on your subscription → no key.
- **Alumnium `do/check` model** — needs a provider key. We use **OpenAI** (their run used
  GPT-5 Nano via Azure; OpenAI is equivalent + one fewer dependency).
- **Judge** — our subscription Claude judge (`../webvoyager/evaluate.py`) → no key.

```bash
export OPENAI_API_KEY=sk-...
# or (kept out of git): echo 'OPENAI_API_KEY=sk-...' > ../webvoyager/.env
```

## Option A — adapted, single key (recommended)
Reuses our harness; Alumnium MCP is the browser layer, Claude judges. From `../webvoyager`:
```bash
cd ../webvoyager
python download_data.py                       # once (shared with the agent-browser harness)
python alumnium_run.py --per-site 3 --concurrency 4   # subset
python alumnium_run.py --concurrency 4                # full set (resumable)
python report.py alumnium                             # summary
```
Then list the failures:
```bash
cd ../alumnium
python extract_failures.py ../webvoyager/results/alumnium
```

## Option B — exact fidelity (their harness verbatim)
For a literal reproduction (their `run_claude_code.py` + GPT-5 `auto_eval.py`, which also
captures per-step screenshots). Needs OpenAI and matches their Azure-style config.
```bash
./setup.sh                                    # clone alumnium-hq/alumnium + WebVoyager fork
# then run their runner per task under vendor/alumnium/benchmarks/webvoyager/:
#   python run_claude_code.py --list-tasks
#   python run_claude_code.py Allrecipes--0
# and their evaluator:
#   python evaluation/auto_eval.py --api_key $OPENAI_API_KEY --api_model gpt-5-chat
python extract_failures.py vendor/alumnium/benchmarks/webvoyager/results/claude-code
```

## Deviations to note in the paper
Option A swaps Azure→OpenAI for the do/check model and GPT-5→Claude for the judge, and has
no per-step screenshots (Alumnium MCP exposes no screenshot tool, so the judge scores the
answer against the reference). Option B removes those deviations. The verdict parser in
`extract_failures.py` is tolerant of both output formats.
