# agent-qa

## Roadmap
- [x] Related work exploration
- [ ] Run the benchmarks (SOTA vs agent-browser based pipeline). We will identify the bugs and where are the failures, and from the failures we can brainstorm some ideas.
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
```
Run a benchmark from the repo root, e.g. `python -m benchmarks.webvoyager.run --ids ArXiv--0`.
See `benchmarks/webvoyager/README.md`. Roadmap for adding BrowseComp + WebArena:
`tickets/0001-restructure-benchmarks-by-benchmark-and-extract-core.md`.