"""Wire WebArena onto `core`: tasks + runner + DETERMINISTIC judge + the reset environment.

The two seams that make WebArena the stress test for `core/`:
  - judge = `evaluate.JUDGE` (deterministic, `is_deterministic=True`) — `core.run` scores once,
    never re-judges, and the judge imports none of the `claude -p` plumbing.
  - environment = `webarena_environment` — per-task state reset (the first non-trivial impl).

Serial only: tasks mutate ONE shared stack, so concurrency would corrupt the per-task reset.
Replicate the stack per worker to parallelize (documented in the README).
"""
from benchmarks.webarena import config, evaluate, tasks
from benchmarks.webarena.env.stack import webarena_environment
from benchmarks.webarena.runners import agent_browser, colorbrowser
from core.run import Benchmark, Runner


def build():
    runners = {
        # Our pipeline: Claude Code + agent-browser (DOM/accessibility-tree) in a Steel container.
        "agent_browser": Runner(
            name="agent_browser",
            run=agent_browser.run,
            environment=webarena_environment,   # per-task DB-snapshot reset
            needs_browser=False,                 # runner brings its own Steel agent-browser
            concurrency_safe=False,              # shared stack + reset -> serial
        ),
        # THIRD-PARTY SOTA comparator: ColorBrowserAgent (MadeAgents, 71.2%) — a different agent
        # SYSTEM (BrowserGym, GPT-5), self-evaluating (cum_reward), so core uses its own verdict.
        "colorbrowser": Runner(
            name="colorbrowser",
            run=colorbrowser.run,
            environment=webarena_environment,    # per-task DB-snapshot reset (shared stack)
            needs_browser=False,                 # ColorBrowserAgent brings its own Chromium
            concurrency_safe=False,              # shared stack + reset -> serial
            preflight=colorbrowser.preflight,
            self_eval=True,                      # uses its own deterministic eval (cum_reward)
        ),
    }
    return Benchmark(
        name="WebArena",
        results_dir=config.RESULTS_DIR,
        load_tasks=tasks.load_tasks,
        load_refs=tasks.load_refs,
        bucket_of=tasks.bucket_of,
        runners=runners,
        judge=evaluate.JUDGE,
        default_runner="agent_browser",
    )
