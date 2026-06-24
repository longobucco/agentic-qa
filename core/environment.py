"""Per-task world setup/teardown — the lifecycle seam.

`with runner.environment(task, port=...) as env:` yields whatever the runner needs to act,
and tears it down on exit. The shape of "the world" differs sharply by benchmark, which is
exactly why it's a seam and not an assumption baked into the run loop:

  - **Live-site, runner brings its own browser** (Alumnium MCP, web-search runners): nothing
    to set up -> `null_environment`.
  - **Live-site, harness owns the browser** (WebVoyager agent-browser): launch a throwaway
    Chrome on `port` -> `live_site_environment`.
  - **Self-hosted** (WebArena, future): bring up the site stack + reset per-task state. That
    impl will live in the benchmark's `env/` and yield reset hooks alongside the endpoint.

Every impl yields an `Env`; runners read only the fields they need.
"""
from contextlib import contextmanager
from dataclasses import dataclass

from core.browser import Chrome


@dataclass
class Env:
    """What a runner receives for one task. `port` is a CDP debug port, or None."""
    port: int = None
    browser: object = None


@contextmanager
def null_environment(task, *, port=None):
    """No world to provision (the runner brings its own browser, e.g. via MCP)."""
    yield Env(port=None)


def live_site_environment(find_chrome, headless=True):
    """Factory -> a per-task CM that launches a throwaway Chrome and yields its CDP port.

    `find_chrome` is called lazily *inside* the CM (not at wiring time) so that selecting a
    runner which needs no browser never forces Chrome to be installed. The live-site impl is
    deliberately a thin wrapper over `core.browser`.
    """
    @contextmanager
    def _env(task, *, port):
        with Chrome(port, chrome_bin=find_chrome(), headless=headless) as browser:
            yield Env(port=port, browser=browser)
    return _env
