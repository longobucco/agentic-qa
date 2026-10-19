# agent-qa

## Roadmap
- [x] Related work exploration
- [x] Run the benchmarks (SOTA vs agent-browser based pipeline). We will identify the bugs and where are the failures, and from the failures we can brainstorm some ideas.
- [ ] Ideas to explore and do some experiments
- [ ] Validate our ideas and solutions (benchmark)
- [ ] Write the paper
- [ ] Publish the paper

## Layout
```
core/                  reusable harness: browser, agent loop, judge seam, environment,
                       results, reporting, tasks, run loop — benchmark-agnostic
benchmarks/<name>/     one dir per benchmark = its tasks + prompts + judge + runners,
                       built on core/. "Which agent" is a runner (--system), not a dir.
  webvoyager/          live-site tasks, LLM screenshot judge; runners: agent_browser, alumnium
  browsecomp/          live-web research questions, answer-vs-reference judge
  webarena/            self-hosted site stack, deterministic checker
  osworld/             OSWorld-Verified desktop tasks (Daytona sandboxes), official protocol,
                       OSWorld's own evaluators; runners: Claude Code, GPT Astra via Codex
scripts/               campaign drivers (OSWorld 361 x 5 driver)
```
Run a benchmark from the repo root, e.g. `python -m benchmarks.webvoyager.run --ids ArXiv--0`.
Each benchmark's README documents its setup, runners and results; OSWorld results and their
analysis: `benchmarks/osworld/README.md` and `benchmarks/osworld/campaign_analysis/README.md`.
