# OSWorld

OSWorld (real Ubuntu desktop tasks) on the shared `core/`. Brain is `claude -p`; the hands are an
MCP server wrapping OSWorld's in-guest controller (screenshot + pyautogui); the judge is OSWorld's
own deterministic evaluator. No API keys.

## Layout
- `runners/agent_computer.py` — `claude -p` + MCP; scores with OSWorld's evaluators while live
- `env/osworld_eval.py` — delegates scoring to OSWorld's official evaluators (desktop_env)
- `env/sandbox.py` — one Daytona desktop per task (+ `up`/`down`/`list` CLI)
- `env/controller.py` — HTTP client to the in-guest controller
- `mcp/server.py` — the desktop tools (screenshot/click/type/key/run_python/...)
- `evaluate.py` — fallback when the official evaluator is unreachable; always `EVAL_ERROR`, never a guessed verdict
- `docker/Dockerfile.osworld` — the desktop image (base for the sandbox)

## Setup
```
pip install mcp daytona_sdk
docker build --platform linux/amd64 -f benchmarks/osworld/docker/Dockerfile.osworld -t <registry>/osworld-ab:latest .
docker push <registry>/osworld-ab:latest
docker inspect <registry>/osworld-ab:latest --format '{{index .RepoDigests 0}}'   # -> pin THIS
export OSW_IMAGE=<registry>/osworld-ab@sha256:...      # DAYTONA_API_KEY goes in the repo .env
python -m benchmarks.osworld.data.download_data
```
`config.py`'s default `IMAGE` is already pinned to a known-good digest; only override `OSW_IMAGE` if
you rebuild the image yourself (see the Notes below on why `:latest` alone isn't safe here).

## Run
```
python -m benchmarks.osworld.env.sandbox up               # provision a desktop
python -m benchmarks.osworld.run --per-bucket 1 --limit 6
python -m benchmarks.osworld.report
```
Resume a sandbox Daytona auto-stopped (see Notes below):
```
python -m benchmarks.osworld.env.sandbox resume <sandbox-id>
```
Spike without provisioning (drive a desktop you already have up):
```
OSW_CONTROLLER_URL=http://<url>:5000 python -m benchmarks.osworld.run --ids <task-id>
```

## Notes
- `OSW_RELEASE=verified` only (369 tasks, xlang-ai/OSWorld). `v2` isn't wired up — different
  schema, own `desktop_env` (see `data/download_data.py`) — and fails loudly rather than silently
  downloading the wrong data.
- Serial: one Daytona sandbox per task, `concurrency_safe=False`. Disk caps at 10GB.
- Scoring requires `desktop_env` importable. `evaluate.py` is the fallback and only ever reports
  `EVAL_ERROR` — no reimplemented metric is verified to agree with the real one (see below), so it
  never produces a SUCCESS/FAILURE that could pass as an official verdict.
- Daytona can auto-stop a sandbox well before `auto_stop_interval`, and never auto-restarts the
  controller (its own init owns PID 1, not this image's CMD) — `env/sandbox.py` handles both on
  every path (`provision()`, `OSW_SANDBOX_ID` reuse, `resume` CLI).
- `config.IMAGE` is pinned by digest, not `:latest` — Daytona caches by tag, so a mutable tag can
  silently serve a stale build. Update the digest on every rebuild+push.
- Task set (`data/download_data.py::UPSTREAM_COMMIT`) and guest server image
  (`docker/Dockerfile.osworld`'s `OSW_UPSTREAM_COMMIT`) are pinned to the same upstream commit, not
  `main` HEAD. Bump both together.
- `OSW_MAX_TURNS=150` / `OSW_MAX_STEPS=30` / `OSW_TASK_TIMEOUT=3600`, pinned independently of the
  browser benchmarks' budget (desktop GUI turns run more per action).
- `result.json` records `agent_clean_finish` (did the agent print `ANSWER:` without erroring) and
  the raw `claude -p` envelope fields. A SUCCESS with `agent_clean_finish=False` gets a `note` on
  `eval.json` ("incidental" — the desktop state happened to already satisfy the evaluator).
  `report.py` prints the clean-vs-incidental breakdown separately from the pooled rate.
- Three outcomes besides SUCCESS/FAILURE, all excluded from pass-rate/variance/per-bucket stats:
  `ENVIRONMENT_ERROR` (task config setup failed — the agent was never meaningfully run),
  `EVAL_ERROR` (agent ran, scoring itself failed or was never verified trustworthy),
  `INFRA_FLAKE`/`HARNESS_ERROR` (recorded in `infra_error.json`, not `eval.json`, so resume retries
  them). See `core/reporting.py::summarize`.
- `config.SUPPORTED_APPS`: 8 apps validated end to end via the real config path, not just "the
  package installed" — `libreoffice_calc`/`_writer`/`_impress`, `gimp`/`thunderbird`/`vlc`,
  `chrome`/`vscode` — plus the `terminal` capability. 260/369 verified-release tasks are in scope by
  default; `OSW_INCLUDE_ALL_APPS=1` overrides. `"os"` tasks stay out (mixed support: some utilities
  work, others need services this image doesn't run — see `analysis/g1_scope_representativeness.py`
  for the scope-bias audit).
- Chrome/vscode run through a wrapper (`/usr/bin/google-chrome`, `/usr/bin/code`) that injects
  `--no-sandbox --user-data-dir=...`, since neither binary starts as launched by the task's own
  command when run as root.
- `report.py` prints who scored each run (`official` vs `offline_fallback`) — any `offline_fallback`
  run is `EVAL_ERROR`, never blended into the pass-rate.
- Reusing a desktop across tasks (`OSW_CONTROLLER_URL` / `OSW_SANDBOX_ID`) doesn't reset state
  between tasks and warns once per process — don't treat results collected this way as a valid
  pass-rate.
- **Known validity gap, not yet resolved**: `env/osworld_eval.py::evaluate_official` hasn't been
  fully re-audited against upstream `desktop_env.py::DesktopEnv.evaluate()` since the last
  `UPSTREAM_COMMIT` bump. One confirmed divergence: on a multi-metric `conj="or"` evaluator (29/369
  tasks), upstream has an unhandled `FileNotFoundError` case that we handle gracefully instead of
  reproducing — our verdict on those tasks isn't guaranteed identical to what upstream's code would
  produce. Re-audit whenever `UPSTREAM_COMMIT` changes.
- Cost/time: no fixed budget here — pull current numbers from `analysis/g1_pilot_check.py` or
  `report.py` against your own `results/` before sizing a `--runs N` campaign.
