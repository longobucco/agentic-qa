# GPT Astra open-book runner: implementation plan

## Purpose and experiment boundary

This document specifies a reproducible **open-book** OSWorld campaign for
`gpt-6-astra`.  An open-book task may require a browser, an explicit URL, or
external-service setup.  It is deliberately a separate experiment from the
closed-book campaign: its outputs, lock file, readiness checks, retry policy,
and reporting must never be combined with `agent_computer_astra`.

The campaign uses a controlled snapshot/proxy rather than the public Internet.
The agent still interacts only through the OSWorld desktop MCP; it does not get
a host shell, a direct browser automation surface, or a network tool.

### Non-goals

- Do not change the closed-book classifier or rewrite its historical results.
- Do not silently fall back to live Internet access.
- Do not expose proxy credentials, service credentials, evaluator code, gold
  artifacts, or result directories to the agent.
- Do not score a missing proxy, unavailable snapshot, login problem, or setup
  fault as an agent failure.

## Experiment identity

Create a second system name and result root:

```text
system:       agent_computer_astra_openbook
result root:  benchmarks/osworld/results/agent_computer_astra_openbook/
lock:         benchmarks/osworld/astra_openbook_campaign_lock.json
driver:       scripts/g_astra_open_book_driver.sh
retry driver: scripts/g_astra_open_book_retry_driver.sh
manifest:     scripts/g_astra_open_book_ids.txt
```

`agent_computer_astra_openbook` may reuse the Astra Codex runner internals, but
must have its own `preflight()` and result-tree name.  It must reject an ID that
does not classify as `open-book`; the closed-book runner must continue to reject
open-book IDs when its campaign lock is active.

## Frozen population

Generate a manifest once, review it, then lock it.  Each manifest record needs
the classification evidence as well as the task ID; a bare ID list is retained
only as the driver input.

```json
{
  "schema_version": 1,
  "created_at": "2026-09-11T00:00:00Z",
  "source_release": "osworld_verified",
  "classifier": "benchmarks.osworld.closed_book:classify",
  "tasks": [
    {
      "task_id": "...",
      "app": "chrome",
      "book": "open-book",
      "reasons": ["browser_task", "instruction_contains_url"],
      "snapshot_profile": "web-v1"
    }
  ]
}
```

The lock file contains:

- manifest path, task count, ordered-ID SHA-256, and manifest SHA-256;
- OSWorld release, Codex CLI version, requested model, and reasoning effort;
- exact enabled MCP methods and exact disabled Codex features;
- snapshot bundle digest, proxy configuration digest, and image/version ID;
- campaign prompt version/hash and evaluator version/hash;
- whether a task category needs an authenticated mock service.

Any mismatch fails before a desktop is provisioned.  Do not mutate a frozen
manifest in place: a revised snapshot or classifier creates a new campaign ID
and lock.

## Controlled external world

### Snapshot service

Run an internal HTTP(S) snapshot gateway reachable only from the Daytona
desktop.  It maps an allowlisted canonical URL to immutable response fixtures:

```text
desktop Chrome -> snapshot proxy -> immutable bundle (content-addressed)
                              -> optional stateful mock-service namespace
```

The gateway must:

- deny all non-allowlisted outbound domains, including DNS resolution outside
  the test network;
- preserve the URL and browser-visible behavior expected by the task (status,
  redirects, content type, downloads, cookies where required);
- serve a bundle identified by a SHA-256 digest and record the selected bundle
  in the task artifact;
- return an explicit diagnostic page/status for a missing fixture, never proxy
  to the live Internet;
- redact credentials and cookie values from logs.

Use a deterministic clock, locale, timezone, viewport, browser version, and
network latency profile.  Stateful mock services must reset to a declared seed
for *every run*, not merely for every task.

### Credentials and authenticated flows

If a task requires login, provision a short-lived test identity into the
desktop/profile before the agent begins.  Keep the secret outside the prompt,
trajectory, screenshots where possible, and result artifacts.  Record only:

```json
{"auth_profile": "drive-test-user-v1", "auth_ready": true}
```

Google Drive or equivalent live setup is not valid for this campaign.  Model it
with a fixture-backed mock service or defer the task until one exists.

## Per-task lifecycle

Each task/run is a state machine.  Persist every transition in
`lifecycle.jsonl` so a retry is auditable and resumable.

```text
PENDING
  -> PREFLIGHT_OK -> PROVISIONED -> SNAPSHOT_READY -> AGENT_RUNNING
  -> AGENT_FINISHED -> EVALUATING -> SCORED

PREFLIGHT_FAILED / PROVISION_FAILED / SNAPSHOT_UNAVAILABLE
  -> ENVIRONMENT_ERROR
AGENT_RATE_LIMITED -> RETRY_RATE_LIMIT
AGENT_TIMEOUT -> INCONCLUSIVE_TIMEOUT
EVALUATOR_ERROR -> EVAL_ERROR
```

Only `SCORED` means an official evaluator created `eval.json`.  The driver must
not treat an agent output file, a transcript, or a process exit code as
completion.

## Preflight contract

Implement `benchmarks/osworld/open_book_preflight.py` and run it before any
campaign task plus once immediately before each task.  It returns structured
JSON and an exit status, never a prose-only warning.

Campaign-wide checks:

1. Validate the lock, Codex CLI binary/version/login, model and effort.
2. Validate the OSWorld release and the immutable manifest hashes.
3. Verify the snapshot bundle and proxy configuration digests.
4. Verify the Daytona base image contains the required Chrome profile and MCP
   server version.
5. Confirm only the controlled proxy route is available from the desktop.

Task-scoped checks:

1. Reclassify the ID and require `book == "open-book"`.
2. Resolve its declared snapshot profile and required fixture URLs.
3. Probe the fixture via the desktop network namespace, not the host.
4. Reset and health-check the mock-service namespace if one is required.
5. Confirm the declared test identity is installed without printing its value.
6. Emit `preflight.json`; a failure produces `ENVIRONMENT_ERROR` and no agent
   invocation.

The current missing `evaluation_examples/settings/proxy/dataimpulse.json` case
must fail at step 3 with `SNAPSHOT_PROXY_MISSING`; it must never consume three
agent attempts.

## Agent policy

The MCP action set stays identical to the closed-book Astra campaign:

```text
screenshot, a11y_tree, click, double_click, right_click, move,
scroll, type, key, wait
```

Keep shell, direct browser control, external web search, apps, and all other
Codex auxiliary surfaces disabled.  Chrome may access the controlled proxy only
through ordinary desktop interaction.  The prompt states that the browser
environment is a provided task resource, but must not name the snapshot
implementation, fixture paths, evaluator, or reference answers.

## Runner responsibilities

Create `benchmarks/osworld/runners/gpt_astra_openbook.py` by extracting shared
non-policy-specific Astra code where useful.  Its `run()` sequence is:

```python
def run(task, *, env, out, refs=None, dry=False):
    require_open_book(task)
    readiness = open_book_preflight.task_check(task, env)
    write_json(out / "preflight.json", readiness)
    if not readiness["ready"]:
        return write_environment_error(out, readiness)

    reset_fixture_namespace(readiness["run_seed"])
    start = now()
    meta = run_codex_meta(build_codex_cmd(open_book_prompt(task), ...))
    save_transcript_audit_and_telemetry(out, meta)

    if is_rate_limited(meta):
        return write_rate_limited(out, meta)
    if is_infra_error(meta):
        return write_environment_error(out, meta)

    return run_official_evaluator_once(task, env, out, started_at=start)
```

`run_official_evaluator_once` is the existing official evaluator path; it must
not receive snapshot implementation data.  The runner records no success/fail
claim until this call has produced its verdict.

## Result layout and provenance

```text
results/agent_computer_astra_openbook/<task_id>/run_<n>/
  result.json
  eval.json                         # only if official evaluation ran
  infra_error.json                  # only for non-agent infrastructure faults
  preflight.json
  lifecycle.jsonl
  conversation.jsonl
  agent_output.txt
  codex_stderr.txt                  # if non-empty; scrub before writing
  tool_audit.json
  network_provenance.json
  final.png
```

`network_provenance.json` contains only non-sensitive identifiers:

```json
{
  "network_mode": "controlled_snapshot",
  "snapshot_bundle_sha256": "...",
  "snapshot_profile": "web-v1",
  "fixture_namespace": "run-scoped opaque ID",
  "proxy_config_sha256": "...",
  "auth_profile": "drive-test-user-v1",
  "clock_profile": "utc-2026-09-11-v1"
}
```

All redaction happens before persistence.  A scan for known secret patterns is
part of result finalization; detected secrets prevent publication and mark the
run `ARTIFACT_REDACTION_ERROR`.

## Driver and retry semantics

The campaign is serial (`concurrency=1`), because each unit owns a Daytona
desktop and a run-scoped fixture namespace.  The normal driver is resumable:

```bash
scripts/g_astra_open_book_driver.sh
```

For each ID, it performs task preflight, invokes three runs only when no
official verdict exists for that run, and leaves existing official verdicts
immutable.  It maintains separate queues:

| Queue | Admission rule | Action |
|---|---|---|
| completed | `eval.json` exists | skip |
| rate-limited | provider 429/usage limit | exponential global backoff, then retry |
| infrastructure | preflight/provision/snapshot failure | retry only after readiness is fixed |
| evaluator | evaluator error | investigate evaluator; no blind retry |
| agent timeout | agent began but did not finish | bounded retry policy, separately reported |

The retry driver accepts a generated JSON queue, not a hand-edited implicit
list.  Queue generation reads result artifacts and includes the error class,
attempt count, next eligible timestamp, and campaign lock digest.  It refuses a
queue created for another lock.

## Reporting

Add `python -m benchmarks.osworld.report agent_computer_astra_openbook`.  It
reports open-book data independently and includes:

- scored task/run count, success rate, and confidence interval;
- breakdown by app, open-book reason, and snapshot profile;
- inconclusive counts by infrastructure class;
- rate-limit attempts and wall-clock time, excluded from performance metrics;
- snapshot bundle and lock digest used for every aggregate.

Never average or compare this rate directly with the closed-book result as if
they had identical information resources.  A combined paper table may show
them in adjacent labelled columns, with each denominator and exclusion rule.

## Tests

Add unit tests for:

1. manifest generation includes every and only classifier-labelled open-book ID;
2. lock validation fails on ID ordering, bundle, model, policy, or proxy digest
   changes;
3. the runner rejects a closed-book ID before provisioning;
4. absent proxy/fixture/auth produces `ENVIRONMENT_ERROR` and invokes no Codex
   process;
5. a 429 produces `RATE_LIMITED`, no `eval.json`, and an eligible retry item;
6. a successful agent attempt invokes the official evaluator exactly once;
7. fixture namespace reset occurs once per run and preserves deterministic seed;
8. result redaction removes test credentials from persisted artifacts;
9. reporting excludes all non-verdict run directories from success metrics.

Run a canary before the campaign: one browser-only fixture task, one URL task,
and one mocked-login task, each with a verified deterministic re-run.  Freeze
the final lock only after those canaries pass.

## Delivery order

1. Define fixture bundle format, controlled proxy, and Daytona image contract.
2. Implement manifest generator and campaign lock; review the population.
3. Implement preflight and its tests; make missing proxy a hard failure.
4. Add the isolated runner/system/result root and artifact provenance.
5. Add serial driver, generated retry queues, and open-book reporting.
6. Run canaries, compare two identical seeded executions, then freeze the lock.
7. Start the full campaign only after the canary evidence and lock are checked in.

