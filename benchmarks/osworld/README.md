# OSWorld

OSWorld (real Ubuntu desktop tasks) on the shared `core/`, running only the official protocol:
alignment of the harness with the official OSWorld harness (xlang-ai/OSWorld @ `091f5ef`) and
with the protocol published in the Claude Sonnet 5 System Card (361 tasks, 1080p, 100 steps, max
effort, pass@1 over 5 runs). The baseline brain is `claude -p`; an independent replication uses
GPT Astra through `codex exec`. The hands are the same MCP server exposing a single `computer`
tool wrapping OSWorld's in-guest controller (screenshot + pyautogui), and the judge is OSWorld's
own deterministic evaluator.

## Layout
- `runners/agent_computer.py`: `claude -p` + MCP; scores with OSWorld's evaluators while live
- `runners/common.py`: policy-free primitives (MCP config, telemetry, provenance, the post-run
  watchdog, transcript capture, scoring) shared by both runners
- `runners/gpt_astra.py`: `codex exec` + the same MCP/evaluator; separate Astra result tree
- `runners/astra_common.py`: Codex-specific primitives (CLI/model preflight, provenance, rollout
  parsing) shared by `gpt_astra.py`
- `official_protocol.py`: vendored upstream Claude system prompt and DONE/[INFEASIBLE]
  termination rule
- `env/osworld_eval.py`: delegates scoring to OSWorld's official evaluators (desktop_env)
- `env/sandbox.py`: one Daytona desktop per task (+ `up`/`down`/`list`/`resume` CLI)
- `env/controller.py`: HTTP client to the in-guest controller
- `env/http_forwarder.py`, `env/cdp_forwarder.py`: loopback relays so evaluator getters that
  build their own `http://`/CDP URL can reach the guest through the Daytona proxy
- `mcp/server.py`, `mcp/official_computer.py`: the MCP server; serves exactly one tool,
  `computer`, translating actions with upstream's own `parse_actions_from_tool_call`
- `mcp/_upstream_actions.py`: vendored upstream action parsing (generated, do not edit)
- `mcp/probe_server.py`: stub MCP server for `analysis/screenshot_delivery_probe.py`
- `evaluate.py`: fallback when the official evaluator is unreachable; always `EVAL_ERROR`, never
  a guessed verdict
- `benchmark.py`: wires both runners onto `core.run.Benchmark`
- `config.py`: env-derived configuration; refuses Phase 1 knobs and non-official
  protocol/population values at import time; the result-tree naming and the new-infra guard
- `tasks.py`: loads task specs, excludes login tasks, buckets by app
- `report.py`: prints pass rate, mean reward and tool-surface violations for a results tree
- `astra_official361_lock.json`: the frozen Astra campaign lock (model, effort, Codex version,
  population hash, tool policy)
- `analysis/screenshot_delivery_probe.py`: does each agent CLI deliver a full-resolution
  screenshot to the model (local only, no sandbox)
- `data/download_data.py`, `data/download_evaluators.py`: fetch the pinned task set and
  evaluators
- `docker/Dockerfile.osworld`: the desktop image (base for the sandbox)
- `notebooks/dataset_exploration.ipynb`: exploratory notes on the task set

## Setup
```
pip install mcp daytona_sdk
docker build --platform linux/amd64 -f benchmarks/osworld/docker/Dockerfile.osworld -t <registry>/osworld-ab:latest .
docker push <registry>/osworld-ab:latest
docker inspect <registry>/osworld-ab:latest --format '{{index .RepoDigests 0}}'   # -> pin THIS
export OSW_IMAGE=<registry>/osworld-ab@sha256:...      # DAYTONA_API_KEY goes in the repo .env
python -m benchmarks.osworld.data.download_data
python -m benchmarks.osworld.data.download_evaluators
```
`config.py`'s default `IMAGE` is already pinned to a known-good digest; only override `OSW_IMAGE`
if you rebuild the image yourself.

`download_evaluators` fetches `desktop_env/evaluators/` at the same upstream commit as the tasks
and the guest image; scoring uses it in preference to the installed `desktop_env` release and
records which one produced each verdict (`provenance.evaluator_commit`). Skip it and scoring
falls back to the installed release, which cannot score six of the verified tasks at all.
The pinned evaluators are mandatory: config refuses to start with `OSW_PINNED_EVALUATORS=0`.

## Running the official protocol

Both CLIs run the same protocol: the MCP server exposes one `computer` tool, the upstream
Claude system prompt is vendored verbatim in `official_protocol.py`, termination follows the
upstream rule (`[INFEASIBLE]` / fail action -> FAIL, else DONE), and CLI sessions are isolated
(empty temp cwd; no user settings, hooks, plugins or memory; built-in tools denied; pinned CLI
versions). Use of any tool other than `computer` is a terminal FAILURE, never re-run;
unverifiable runs and missing MCP servers are retryable infra errors, never scored.

The population is always the published OSWorld-Verified set minus the 8 login tasks (361 of
369) -- `OSW_POPULATION` may only assert `verified361` (or be unset).

### Claude Code (`agent_computer`)

```
export OSW_MODEL=claude-sonnet-5      # required: preflight refuses to run unpinned
python -m benchmarks.osworld.env.sandbox up               # provision a desktop
python -m benchmarks.osworld.run --per-bucket 1 --limit 6
python -m benchmarks.osworld.report
```

Results land under `results/agent_computer_sonnet5_official/` -- the results tree is named
`agent_computer_<model-slug>[_effort<x>][_<suffix>]_official[_kvm]`. `config.assert_new_infra_system`
guards every tree `benchmark.build()` and `report.py` write or read: only a name matching that
pattern is accepted; any Phase 1 tree name (e.g. `agent_computer`, `agent_computer_astra`,
`verify_replan_sonnet5`) is refused with a pointer to the tag `phase1-daytona-frozen`.

### GPT Astra (`agent_computer_gpt6astra_..._official`, requires a logged-in `codex` CLI)

```
python -m benchmarks.osworld.run --system agent_computer_gpt6astra_max_codex01534_official \
  --per-bucket 1 --limit 6
python -m benchmarks.osworld.report agent_computer_gpt6astra_max_codex01534_official
```

The model, effort and Codex CLI default to `gpt-6-astra`, `max` and `0.153.4`; the preflight
validates all three (plus the tool policy and population hash) against
`astra_official361_lock.json` and refuses to start on any mismatch. Non-default model/effort
combinations get a different result-tree name automatically, so a campaign override never pools
into or overwrites another one's results. Codex's JSONL trajectory is saved directly as
`conversation.jsonl`. The runner disables Codex's built-in shell/browser/computer/app and
auxiliary tool surfaces and injects only the OSWorld MCP; the sandboxed Code Mode host remains
enabled because Astra uses it to invoke MCP tools. Approval prompts are bypassed because this is
an unattended benchmark; this does not widen the tool surface, and the disposable Daytona
desktop is the external sandbox.

Resume a sandbox Daytona auto-stopped:
```
python -m benchmarks.osworld.env.sandbox resume <sandbox-id>
```
Spike without provisioning (drive a desktop you already have up):
```
OSW_CONTROLLER_URL=http://<url>:5000 python -m benchmarks.osworld.run --ids <task-id>
```

## Campaigns

`scripts/g_official361_driver.py` runs the full official-protocol campaign (361 tasks x 5 runs)
against the Daytona backend end to end -- parallel shards, quota-safe, preflight first. The
rules (rounds, resume, backoff, stop, signals) live in `benchmarks/osworld/campaign.py`; the
backend module (`benchmarks/osworld/campaign_daytona.py`) supplies its own protocol variables,
extra refusals and the sweep of its own orphaned sandboxes.

```
BACKEND=daytona ARM=sonnet|astra PARALLEL=K MAX_HOURS=H \
  .venv/bin/python scripts/g_official361_driver.py
... --dry-run    # print the planned batches, run nothing
```

`BACKEND=daytona` is the only backend wired up on this branch. For `ARM=astra`,
`OSW_ASTRA_REASONING_EFFORT` must be set explicitly by the caller -- it is a campaign decision,
never a default. The results tree is named `agent_computer_sonnet5_effortmax_protocol361_official`
for Sonnet, or `agent_computer_gpt6astra_<effort>_codex01534_protocol361_official` for Astra.

The driver resumes from disk: a restarted process re-derives its pending `(task, run)` units from
what's already on disk (`core.results.is_done`), so no `--force` flag or manual bookkeeping is
needed. If >= 50% of the units attempted in a round were rate-limited, it backs off 30 minutes
(quota windows reset on the order of hours) before trying again. It stops (exit 3) on a systemic
or repeated non-quota failure, an `AUTH_ERROR`, or a unit already stuck from an earlier session --
see the module docstring in `campaign.py` for the exact rules.

`OSW_IMAGE` must be left unset: the driver's harness-knob table pins it at `""`, which means
`config.IMAGE`'s pinned digest; any other value is refused before a single sandbox starts. Every
sandbox a driver child provisions is labelled with that driver run's id
(`env/sandbox.DRIVER_RUN_LABEL`), and after every round and on exit the driver deletes only the
sandboxes carrying its own run id -- another driver's (or a manually started) sandbox is never
touched, even if the server-side label filter were ever to leak one.

Everything the driver and its children print goes to `scripts/g_official361_<arm>_daytona.log`.

## Reporting

`python -m benchmarks.osworld.report [<system>]` (defaults to the current pinned system) prints:
- the pass rate and per-bucket breakdown (`core.reporting.summarize`);
- OSWorld's own score, mean reward with partial credit, next to the binary success rate
  (`official_mean_reward`/`_mean_reward_line`) -- the number comparable with published OSWorld
  scores;
- the clean-vs-incidental breakdown (a SUCCESS whose agent didn't cleanly finish);
- tool-surface violations: runs the official protocol scored a terminal FAILURE because the
  agent used a tool other than `computer` (`tool_surface_violation_count`), reported separately
  so it stays distinguishable from an ordinary agent FAILURE.

`report.py --compare <system-a> <system-b>` runs an A/B comparison across two result trees.

## Phase 1

The earlier Daytona harness -- its arms (verify-replan, grounding, open-book), drivers and tests
-- is frozen under the git tag `phase1-daytona-frozen`. Its results are not in git. This branch
carries only the official-fidelity infrastructure described above.

## Notes
- `OSW_RELEASE=verified` only (369 tasks, xlang-ai/OSWorld). `v2` isn't wired up (different
  schema, own `desktop_env`, see `data/download_data.py`) and fails loudly rather than silently
  downloading the wrong data.
- Serial: one Daytona sandbox per task, `concurrency_safe=False`. Disk caps at 10GB.
- Three things are pinned to one upstream commit, and they have to be bumped together: the task
  set (`data/download_data.py::UPSTREAM_COMMIT`), the guest server image
  (`docker/Dockerfile.osworld`) and the evaluators themselves (`data/download_evaluators.py`).
- Upstream getters that build their own `http://{ip}:{port}` URL reach the guest through a
  loopback forwarder (`env/http_forwarder.py`); a Chrome CDP getter needing `ws://`/`http://`
  discovery of its own is served the same way through `env/cdp_forwarder.py`.
- Scoring requires `desktop_env` importable. `evaluate.py` is the fallback and only ever reports
  `EVAL_ERROR`: no reimplemented metric is verified to agree with the real one, so it never
  produces a SUCCESS/FAILURE that could pass as an official verdict.
- Daytona can auto-stop a sandbox well before `auto_stop_interval`, and never auto-restarts the
  controller (its own init owns PID 1, not this image's CMD); `env/sandbox.py` handles both on
  every path (`provision()`, `OSW_SANDBOX_ID` reuse, `resume` CLI).
- `config.IMAGE` is pinned by digest, not `:latest`: Daytona caches by tag, so a mutable tag can
  silently serve a stale build. Update the digest on every rebuild+push.
- `OSW_MAX_STEPS=100` / `OSW_TASK_TIMEOUT=14400` (upstream's official-protocol step budget and
  task timeout), pinned independently of the browser benchmarks' budget.
- `result.json` records `agent_clean_finish` (did the agent print `ANSWER:` without erroring).
  A SUCCESS with `agent_clean_finish=False` gets a `note` on `eval.json` ("incidental": the
  desktop state happened to already satisfy the evaluator). `report.py` prints the
  clean-vs-incidental breakdown separately from the pooled rate.
- Three outcomes besides SUCCESS/FAILURE, all excluded from pass-rate/variance/per-bucket stats:
  `ENVIRONMENT_ERROR` (task config setup failed; the agent was never meaningfully run),
  `EVAL_ERROR` (agent ran, scoring itself failed or was never verified trustworthy),
  `INFRA_FLAKE`/`HARNESS_ERROR` (recorded in `infra_error.json`, not `eval.json`, so resume
  retries them). See `core/reporting.py::summarize`.
- Chrome/vscode run through a wrapper (`/usr/bin/google-chrome`, `/usr/bin/code`) that injects
  `--no-sandbox --user-data-dir=...`, since neither binary starts as launched by the task's own
  command when run as root.
- `report.py` prints who scored each run (`official` vs `offline_fallback`); any
  `offline_fallback` run is `EVAL_ERROR`, never blended into the pass-rate.
- Reusing a desktop across tasks (`OSW_CONTROLLER_URL` / `OSW_SANDBOX_ID`) doesn't reset state
  between tasks and warns once per process; don't treat results collected this way as a valid
  pass-rate.
- Cost/time: no fixed budget here; pull current numbers from `report.py` against your own
  `results/` before sizing a `--runs N` campaign.
