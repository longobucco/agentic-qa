"""Provision a WebArena site as a Daytona x86 sandbox and expose it via a public URL.

WebArena's stack is amd64 + multi-GB + needs per-task DB reset, so it can't run on an arm64
laptop. Daytona gives us a native x86 sandbox; created **from a site image** with `public=True`
it gets a public preview URL we point the harness at via `WA_<SITE>_URL`. Reset/start/inspect
all run through the same sandbox's exec channel.

Architecture (empirically settled — see `env/provision.md` and `env/_probe*.py`):
  - Per-sandbox disk caps at **10GB** and Daytona Volumes are FUSE/S3 that reject the `chmod`
    docker's data-root needs — so the ticket's "docker data-root on a Volume" split is DEAD.
  - The path that works: **create the sandbox FROM the 45GB image** (image = read-only base;
    the 10GB overlay takes Postgres writes, which overlay *can* fsync). Daytona accepts the
    big base without counting it against the 10GB writable cap.
  - Daytona runs its OWN init, not the image CMD, so the site's services are started by hand
    after create (`start_cmd` here, or `daytona exec <id> '<cmd>'`).

Validated Daytona primitives (this module wraps them):
  - `Daytona(DaytonaConfig(api_key=...))`; `daytona.create(CreateSandboxFromImageParams(...))`
  - `Resources(cpu<=4, memory, disk<=10)`; `sandbox.get_preview_link(port).url` -> public URL
  - `sandbox.process.exec(cmd)` -> run/reset/inspect; `sandbox.delete()` -> teardown

Usage:
  python -m benchmarks.webarena.env.daytona up    reddit <image-ref> 80
  python -m benchmarks.webarena.env.daytona exec  <sandbox-id> 'cat /start.sh'
  python -m benchmarks.webarena.env.daytona reset <sandbox-id> reddit
  python -m benchmarks.webarena.env.daytona down  <sandbox-id>
  python -m benchmarks.webarena.env.daytona list
"""
import os
import sys
import time

from core.dotenv import load_dotenv   # shared repo-root .env loader (DAYTONA/OPENAI/...)

# Per-site START — Daytona runs its OWN init, not the image CMD, so the site's services don't
# auto-start. The WebArena "exposed" images end their entrypoint with `exec "$@"` (the CMD =
# supervisord); starting supervisord directly brings up nginx + php-fpm + postgres. supervisord
# DAEMONIZES, so a one-shot exec persists (a foreground entrypoint would die with the exec call).
# VERIFIED LIVE 2026-06-25: reddit -> nginx/php-fpm/postgres RUNNING, localhost:80 -> 200.
START_CMDS = {
    "reddit": "supervisord -c /etc/supervisord.conf",
    # shopping / shopping_admin / gitlab use the same supervisord pattern; confirm the conf path.
}

# Per-site state reset run INSIDE the sandbox (restore the seed DB baked into the image). Paths
# follow the upstream WebArena images; adjust per image if its dump lives elsewhere. Reddit =
# Postmill on Postgres. `daytona reset <id> <site>` execs these; wire it via WA_RESET_<SITE>.
RESET_CMDS = {
    "reddit": (
        "psql -U postmill -d postmill -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' && "
        "psql -U postmill -d postmill -f /var/lib/postgresql/seed.sql"
    ),
}


def _client():
    load_dotenv()   # repo-root .env -> DAYTONA_API_KEY (shared with all benchmarks)
    from daytona_sdk import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))


def _exec(sandbox, cmd, timeout=300):
    r = sandbox.process.exec(f"bash -lc {_q(cmd)}", timeout=timeout)
    return getattr(r, "exit_code", None), (getattr(r, "result", "") or "").strip()


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _get(d, sandbox_id):
    for s in d.list():
        if s.id == sandbox_id:
            return s
    raise SystemExit(f"sandbox {sandbox_id} not found")


def provision_site(image, port, *, disk=10, memory=8, cpu=4, start_cmd=None, ready_path="/",
                   auto_stop=20):
    """Create a public sandbox FROM `image` (image = read-only base, 10GB writable overlay),
    optionally start the site's services, and return (sandbox, public_url). Caller tears it
    down with teardown(sandbox). `auto_stop` (minutes) stops an abandoned sandbox for cost
    safety. Disk is capped at 10GB by Daytona — do NOT raise it (the image rides as base)."""
    from daytona_sdk import CreateSandboxFromImageParams, Resources
    d = _client()
    sb = d.create(CreateSandboxFromImageParams(
        image=image, public=True,
        resources=Resources(cpu=cpu, memory=memory, disk=min(disk, 10)),
        auto_stop_interval=auto_stop,
    ), timeout=2400)                      # 45GB pull is server-side; allow time
    if start_cmd:
        # supervisord daemonizes, so a plain exec starts it and RETURNS while it keeps running.
        # (A one-shot detached `&` of the image entrypoint does NOT persist — Daytona reaps the
        # exec's process group; supervisord forking itself is what survives.)
        _exec(sb, start_cmd, timeout=120)
    url = sb.get_preview_link(port).url
    for _ in range(90):                   # poll readiness from inside the sandbox
        c, o = _exec(sb, f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}{ready_path}")
        if o.strip().startswith(("2", "3")):
            break
        time.sleep(2)
    return sb, url


def reset_site(sandbox, site):
    """Run a site's per-task reset (DB restore) inside its sandbox. Returns (exit_code, out)."""
    cmd = RESET_CMDS.get(site)
    if not cmd:
        raise SystemExit(f"no reset command known for site '{site}' (add it to RESET_CMDS)")
    return _exec(sandbox, cmd, timeout=900)


def teardown(sandbox):
    sandbox.delete()


def _main(argv):
    if not argv:
        print(__doc__); return
    cmd = argv[0]
    if cmd == "up":
        _, site, image, port = argv[0], argv[1], argv[2], int(argv[3])
        sb, url = provision_site(image, port, start_cmd=START_CMDS.get(site))
        print(f"sandbox={sb.id}\nWA_{site.upper()}_URL={url}")
    elif cmd == "exec":
        d = _client()
        code, out = _exec(_get(d, argv[1]), argv[2])
        print(out)
        sys.exit(0 if code in (0, None) else 1)
    elif cmd == "reset":
        d = _client()
        code, out = reset_site(_get(d, argv[1]), argv[2])
        print(out or "(reset ok)")
        sys.exit(0 if code in (0, None) else 1)
    elif cmd == "down":
        d = _client()
        _get(d, argv[1]).delete()
        print("deleted", argv[1])
    elif cmd == "list":
        for s in _client().list():
            print(s.id, getattr(s, "state", None))
    else:
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
