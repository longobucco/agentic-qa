"""WebArena's environment lifecycle — the first NON-trivial `core.environment` impl.

For live-site benchmarks the environment is a no-op; WebArena is the case the seam was added
for: **per-task state reset**. WebArena tasks mutate a self-hosted stack (add to cart, open a
gitlab issue, post to the forum), so without resetting the relevant site between tasks the
results contaminate each other. This context manager runs that reset before each task.

The reset is site-specific (restore a DB snapshot) and depends on how you provisioned the
stack, so it's injected via env: `WA_RESET_<SITE>` is a shell command that restores that
site, e.g.

    WA_RESET_REDDIT="docker exec wa-reddit-db psql -U postmill -c 'ROLLBACK; \\i /snap.sql'"

If a site has no reset configured, we WARN (rather than silently contaminate). Provision and
reset commands live in `benchmarks/webarena/env/provision.md`. Because reset mutates one
shared stack, the runner is **serial** unless you replicate the stack per worker.
"""
import os
import subprocess
from contextlib import contextmanager

from core.environment import Env


def reset_site(site):
    """Restore one site's state from its snapshot. Returns True if a reset ran."""
    cmd = os.environ.get(f"WA_RESET_{site.upper()}")
    if not cmd:
        print(f"[webarena] WARNING: no WA_RESET_{site.upper()} set — '{site}' NOT reset; "
              f"results may contaminate across tasks.")
        return False
    subprocess.run(cmd, shell=True, timeout=900, capture_output=True)
    return True


@contextmanager
def webarena_environment(task, *, port=None):
    """Reset every site this task touches, then yield. The runner brings its own browser
    (Steel agent-browser), so no `port` is provisioned here — the world is the hosted stack."""
    for site in (task.get("sites") or []):
        reset_site(site)
    yield Env(port=None)
