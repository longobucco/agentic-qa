# OSWorld

OSWorld (real Ubuntu desktop tasks) on the shared `core/`. Brain is `claude -p`; the hands are an
MCP server wrapping OSWorld's in-guest controller (screenshot + pyautogui); the judge is OSWorld's
own deterministic evaluator. No API keys.

## Layout
- `runners/agent_computer.py`: `claude -p` + MCP; scores with OSWorld's evaluators while live
- `env/osworld_eval.py`: delegates scoring to OSWorld's official evaluators (desktop_env)
- `env/sandbox.py`: one Daytona desktop per task (+ `up`/`down`/`list` CLI)
- `env/controller.py`: HTTP client to the in-guest controller
- `mcp/server.py`: the desktop tools (screenshot/click/type/key/run_python/...)
- `evaluate.py`: fallback when the official evaluator is unreachable; always `EVAL_ERROR`, never a guessed verdict
- `docker/Dockerfile.osworld`: the desktop image (base for the sandbox)

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
`download_evaluators` fetches `desktop_env/evaluators/` at the same `UPSTREAM_COMMIT` as the
tasks and the guest image; scoring uses it in preference to the installed `desktop_env` release
and records which one produced each verdict (`provenance.evaluator_commit`). Skip it and
scoring falls back to the installed release, which cannot score six of the verified tasks at
all. `OSW_PINNED_EVALUATORS=0` forces the installed release back.
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

## Analysis

`analysis/` holds the thesis's validity-audit and results-analysis scripts (`gap-research-plan.md`
tracks the full research design). Every module that reads `results/` picks the tree the same way
the runners write it: `OSW_MODEL=claude-sonnet-5 python -m benchmarks.osworld.analysis.<name>`
reads `results/agent_computer_sonnet5/`, or pass `--system agent_computer_sonnet5` explicitly.
With neither, they read the historical unpinned `agent_computer/` tree. Each is independently runnable as `python -m
benchmarks.osworld.analysis.<name>`; "offline" means no sandbox and no agent cost, "live" means it
needs a provisioned sandbox but still spends no agent/LLM cost, and "re-derived from raw" means it
recomputes purely from `results/` with no new runs.

- `g0_evaluator_audit.py` (G0): static audit of all 369 evaluator specs (offline). Classifies each
  by strength and status against our own rubric, since OSWorld itself only exposes a `[0,1]` score.
- `g0_brittleness.py` (G0 brittleness probe, live, zero agent cost): sets a task's initial state,
  does not act, and scores with the official evaluator; a SUCCESS on an untouched desktop is a
  measured false positive.
- `g0_replication_fidelity.py` (G0.5 static half, offline): does our evaluator replica
  (`env/osworld_eval.py`) dispatch like upstream `desktop_env.py`'s `DesktopEnv.evaluate()`?
- `g0_replication_fidelity_live.py` (G0.5 live half, live, zero agent/LLM cost): same no-op-probe
  method as `g0_brittleness.py`, comparing our `evaluate_official()` against a real `DesktopEnv`
  instance on the same live state.
- `g1_pilot_check.py` (G-1 pilot exit criterion, offline): can every costly-phase deliverable be
  derived from the raw records alone, checked on a small pilot before a full campaign is paid for.
- `g1_scope_representativeness.py` (G1, offline): is the in-scope task subset
  (`config.SUPPORTED_APPS`) representative of the full 369, or systematically easier/harder than
  what's excluded.
- `g2_contamination.py` (G2 contamination half): one no-tools `claude -p` call per sampled task,
  checking whether the model already "knows" a task's ground truth from training data.
- `g2_temporal_oracle.py` (G2 temporal half, offline): static fingerprint of the 369 task JSON for
  oracle state that can drift or break without anyone touching it (e.g. a config step that fetches
  a file by URL).
- `g3_sample.py` (G3): stratified sample, app × evaluator-class, 2 tasks/cell, deterministic;
  excludes evaluators known to be permanently broken.
- `g3_anomaly_scan.py` (G3 anomaly scan, re-derived from raw, zero cost): offline pre-flight sweep
  of recorded raw data for anomaly classes before committing spend to G3-full.
- `g3_full_sample.py` (G3-full): every eligible task rather than just the stratified k=40 sample,
  for a tighter Wilson CI on the benchmark-level pass rate.
- `g4_ablation.py` (G4, re-derived from raw, no new runs): recomputes the pass rate under both the
  ENVIRONMENT_ERROR-excluded (this repo's default) and ENVIRONMENT_ERROR-as-FAILURE (upstream)
  conventions, and reports the gap between them, at both the pre-registered-sample and
  full-population scope.
- `g5_sample.py` (G5): pre-registered scaffold-ablation sample. Real spend, explicitly gated:
  strata and budget are frozen before the run; not launched by default.
- `g6_qa_construct_mapping.py` (G6, offline, analytical): maps each task onto a QA-testing
  competency taxonomy straight from the task JSON, no execution data needed.
- `g8_failure_taxonomy.py` (G8, re-derived from raw, no new runs): failure taxonomy over the
  completed-task cohort, classifying every non-SUCCESS run by layer (INFRA/ORACLE/AGENT) and
  clustering failure signatures into a bug catalogue.
- `task_manifest.py`: one row per in-scope task -> `analysis/results/task_manifest[_<model>].{csv,json}`.
- `g9_replication_validity.py` (G9, re-derived from raw, no new runs): which scored runs are
  invalidated by a defect on OUR side rather than by the agent, split into VALID /
  INVALID_OURS / UNRESOLVED / EXCLUDED, with the task-id list to re-run once each defect is
  fixed (`--write-ids <path>`). The set of getters that build their own `http://` URL is
  derived from the installed `desktop_env` source, so upgrading the library shrinks the
  finding instead of leaving a stale hardcoded list behind.
- `conversation_coverage.py` (re-derived from raw, no new runs): which tasks have a
  `conversation.jsonl` saved and whether it's a real transcript or a rate-limited stub (a
  429 attempt still gets a 9-line stub written, since the CLI still returns a session id to
  copy from). Conversation capture started 2026-08-24, well after the campaign itself, so
  coverage is partial and grows unevenly across backfill batches; this is the source of truth
  for "how many/which tasks have a usable transcript" rather than a number quoted once.
  Manifest saved at `analysis/results/conversation_coverage.json`.

## Notes
- `OSW_RELEASE=verified` only (369 tasks, xlang-ai/OSWorld). `v2` isn't wired up (different
  schema, own `desktop_env`, see `data/download_data.py`) and fails loudly rather than silently
  downloading the wrong data.
- Serial: one Daytona sandbox per task, `concurrency_safe=False`. Disk caps at 10GB.
- Three things are pinned to one upstream commit, and they have to be bumped together: the task
  set (`data/download_data.py::UPSTREAM_COMMIT`), the guest server image
  (`docker/Dockerfile.osworld`) and, since 2026-09-08, the evaluators themselves
  (`data/download_evaluators.py`). The evaluator half was the gap: verdicts were computed by
  whatever `desktop_env` release pip had resolved, and six verified tasks call metrics/getters
  that release doesn't have.
- Upstream getters that build their own `http://{ip}:{port}` URL (12 of them) reach the guest
  through a loopback forwarder (`env/http_forwarder.py`); splitting the https Daytona URL into
  host+port made them talk plain HTTP to port 443, which the proxy answers with `400` and a
  `text/html` body -- `JSONDecodeError` inside the getter, EVAL_ERROR on 20 tasks. Getters that
  want a *different* guest port (Chrome 9222, VLC 8080) are still unreachable: those ports
  aren't published by the sandbox.
- Scoring requires `desktop_env` importable. `evaluate.py` is the fallback and only ever reports
  `EVAL_ERROR`: no reimplemented metric is verified to agree with the real one (see below), so it
  never produces a SUCCESS/FAILURE that could pass as an official verdict.
- Daytona can auto-stop a sandbox well before `auto_stop_interval`, and never auto-restarts the
  controller (its own init owns PID 1, not this image's CMD); `env/sandbox.py` handles both on
  every path (`provision()`, `OSW_SANDBOX_ID` reuse, `resume` CLI).
- `config.IMAGE` is pinned by digest, not `:latest`: Daytona caches by tag, so a mutable tag can
  silently serve a stale build. Update the digest on every rebuild+push.
- Task set (`data/download_data.py::UPSTREAM_COMMIT`) and guest server image
  (`docker/Dockerfile.osworld`'s `OSW_UPSTREAM_COMMIT`) are pinned to the same upstream commit, not
  `main` HEAD. Bump both together.
- `OSW_MAX_TURNS=150` / `OSW_MAX_STEPS=30` / `OSW_TASK_TIMEOUT=3600`, pinned independently of the
  browser benchmarks' budget (desktop GUI turns run more per action).
- `result.json` records `agent_clean_finish` (did the agent print `ANSWER:` without erroring) and
  the raw `claude -p` envelope fields. A SUCCESS with `agent_clean_finish=False` gets a `note` on
  `eval.json` ("incidental": the desktop state happened to already satisfy the evaluator).
  `report.py` prints the clean-vs-incidental breakdown separately from the pooled rate.
- Three outcomes besides SUCCESS/FAILURE, all excluded from pass-rate/variance/per-bucket stats:
  `ENVIRONMENT_ERROR` (task config setup failed; the agent was never meaningfully run),
  `EVAL_ERROR` (agent ran, scoring itself failed or was never verified trustworthy),
  `INFRA_FLAKE`/`HARNESS_ERROR` (recorded in `infra_error.json`, not `eval.json`, so resume retries
  them). See `core/reporting.py::summarize`.
- `config.SUPPORTED_APPS`: 8 apps validated end to end via the real config path, not just "the
  package installed" (`libreoffice_calc`/`_writer`/`_impress`, `gimp`/`thunderbird`/`vlc`,
  `chrome`/`vscode`), plus the `terminal` capability. 260/369 verified-release tasks are in scope by
  default; `OSW_INCLUDE_ALL_APPS=1` overrides. `"os"` tasks stay out (mixed support: some utilities
  work, others need services this image doesn't run; see `analysis/g1_scope_representativeness.py`
  for the scope-bias audit).
- Chrome/vscode run through a wrapper (`/usr/bin/google-chrome`, `/usr/bin/code`) that injects
  `--no-sandbox --user-data-dir=...`, since neither binary starts as launched by the task's own
  command when run as root.
- `report.py` prints who scored each run (`official` vs `offline_fallback`); any `offline_fallback`
  run is `EVAL_ERROR`, never blended into the pass-rate.
- Reusing a desktop across tasks (`OSW_CONTROLLER_URL` / `OSW_SANDBOX_ID`) doesn't reset state
  between tasks and warns once per process; don't treat results collected this way as a valid
  pass-rate.
- **Known validity gap, not yet resolved**: `env/osworld_eval.py::evaluate_official` hasn't been
  fully re-audited against upstream `desktop_env.py::DesktopEnv.evaluate()` since the last
  `UPSTREAM_COMMIT` bump. One confirmed divergence: on a multi-metric `conj="or"` evaluator (29/369
  tasks), upstream has an unhandled `FileNotFoundError` case that we handle gracefully instead of
  reproducing; our verdict on those tasks isn't guaranteed identical to what upstream's code would
  produce. Re-audit whenever `UPSTREAM_COMMIT` changes.
- Cost/time: no fixed budget here; pull current numbers from `analysis/g1_pilot_check.py` or
  `report.py` against your own `results/` before sizing a `--runs N` campaign.
