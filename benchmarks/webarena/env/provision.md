# WebArena stack provisioning + per-task reset (runbook)

`stack.py` resets a site before each task via `WA_RESET_<SITE>`; this file is where the
provisioning + reset commands that back those env vars live. WebArena's sites are **amd64,
multi-GB, with per-task DB reset** — they don't run on the arm64 dev Mac. We host them on an
x86 box (Daytona sandbox or any x86 Docker VM) and point the harness at the public URL via
`WA_<SITE>_URL`.

## What's empirically true about Daytona (measured 2026-06-25, see `env/_probe*.py`)

| Fact | Value | Consequence |
|---|---|---|
| Arch / CPU / RAM | x86_64, **4 vCPU max/sandbox**, up to ~8GB | native, no emulation |
| Per-sandbox **disk cap** | **10GB hard** (20/30/50/60 all rejected) | a 45GB image can't be the writable root |
| Docker-in-sandbox | `apt-get install docker.io` → **dockerd runs** (overlayfs) | DinD is viable inside a sandbox |
| **Volume** (`d.volume`) | `mountpoint-s3` FUSE, ~unlimited, writable | bulk read-only files only |
| Volume + docker data-root | **FAILS**: `chmod … operation not permitted` | docker layers **cannot** live on the S3 volume |
| Volume + Postgres | unusable (FUSE: no fsync/locking) | Postgres **must** sit on the 10GB block root |
| Create **from** the image | **accepted** (not size-rejected); pulls server-side | image as read-only base under a 10GB overlay |

### The conclusion that reshaped Phase 4
The ticket's planned **volume-split** (docker `--data-root` on a Volume for the 45GB image,
Postgres on block) is **dead on this tier**: the S3 volume rejects the `chmod` dockerd does on
its data-root, so image layers can't live there. Two paths survive:

- **(A) Image-as-sandbox-base** — `create(CreateSandboxFromImageParams(image=<45GB site>, …))`.
  The image is the read-only base; the 10GB overlay takes Postgres writes (overlay *does* fsync).
  Daytona accepts the 45GB base (doesn't count it against the 10GB writable cap). This is the
  path `env/daytona.py::provision_site` implements. **Caveat:** Daytona runs its own init, not
  the image's CMD, so the site's services must be started by hand after create (below).
- **(B) Off-Daytona x86 Docker VM** with ≥60GB disk — `docker run` the image directly, no split.
  The harness is identical; only `WA_<SITE>_URL` changes. Use this if (A)'s manual service-start
  or the 45GB pull time fights you, or ask Daytona to raise the per-sandbox disk (support@daytona.io).

## Image sizes (Docker Hub `webarenaimages/*`, compressed)
| site | image | size |
|---|---|---|
| reddit | `postmill-populated-exposed-withimg:latest` | **45.5GB** (only tag; no no-image variant) |
| shopping | `shopping_final_0712:latest` | 45.4GB |
| shopping_admin | `shopping_admin_final_0719:latest` | — |
| gitlab | `gitlab-populated-final:latest` | — |
| map | `nominatim_volumes` (+ ~180GB data) | skip |

## Provision reddit on Daytona (path A) — VERIFIED LIVE 2026-06-25
The 45GB image creates in ~32min, boots, exposes a public URL, and `daytona up` now **auto-starts
the services** (supervisord → nginx + php-fpm + postgres). A real task (`webarena-27`) scored
**SUCCESS** through the deterministic judge against this stack.
```bash
# 1) create from the image (pulls ~45GB server-side, ~30min) AND start services (supervisord).
python -m benchmarks.webarena.env.daytona up reddit \
    webarenaimages/postmill-populated-exposed-withimg:latest 80
#    -> prints: sandbox=<id>   WA_REDDIT_URL=<public preview url>   (services already up; :80 -> 200)

# 2) export the URL + reset for the harness
export WA_REDDIT_URL=<public preview url>
export WA_RESET_REDDIT="python -m benchmarks.webarena.env.daytona reset <id> reddit"
```
Why supervisord and not the image entrypoint: Daytona runs its OWN init, not the image CMD. The
WebArena images' entrypoint ends with `exec "$@"` (CMD = supervisord) — with no CMD it does
nothing. `supervisord` **daemonizes**, so a one-shot exec of it persists; a detached entrypoint
does NOT (Daytona reaps the exec's process group). `START_CMDS` in `daytona.py` encodes this.

## Per-task reset (what `WA_RESET_<SITE>` runs)
WebArena tasks mutate the stack (post, vote, edit), so each site is restored before each task.
For Postmill/reddit the canonical reset is a Postgres restore from the seed dump baked into the
image:
```bash
# inside the sandbox, run by WA_RESET_REDDIT:
psql -U postmill -d postmill -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
psql -U postmill -d postmill -f /var/lib/postgresql/seed.sql        # path varies by image
```
`env/daytona.py::reset_site` wraps this over the sandbox exec channel. If `WA_RESET_<SITE>` is
unset, `stack.py` **warns** rather than silently contaminating results.

## Login (required for most tasks)
Reddit/gitlab/shopping tasks set `require_login`. WebArena's canonical creds (e.g. reddit
`MarvelsGrantMan136` / `MarvelsGrantMan136`) must be entered by the agent or pre-seeded as
cookies. Neither runner pre-authenticates yet — wire credentials into the prompt or seed a
storage-state cookie before running the login-gated buckets. (Tracked as a follow-up.)

## Teardown (always — sandboxes cost money)
```bash
python -m benchmarks.webarena.env.daytona down <id>     # sandbox.delete()
```
`provision_site` also sets `auto_stop_interval` so an abandoned sandbox stops itself.
