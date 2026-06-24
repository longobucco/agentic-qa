"""Wire WebVoyager onto `core`: tasks + runners + judge + buckets -> one `Benchmark`.

This is the whole benchmark definition. `run.py`/`report.py` are one-liners on top of it,
and a new benchmark is the same shape: its own tasks, runners, judge, environment.
"""
from benchmarks.webvoyager import config, evaluate, tasks
from benchmarks.webvoyager.runners import agent_browser, alumnium
from core.environment import live_site_environment, null_environment
from core.run import Benchmark, Runner


def build():
    live = live_site_environment(config.find_chrome, headless=config.HEADLESS)
    runners = {
        # Our agent: harness owns a throwaway Chrome; ONE shared daemon -> serial only.
        "agent_browser": Runner(
            name="agent_browser",
            run=agent_browser.run,
            environment=live,
            needs_browser=True,
            concurrency_safe=False,
        ),
        # Alumnium: each worker gets its own MCP + Selenium Chrome -> parallel-safe, no
        # browser needed from the environment.
        "alumnium": Runner(
            name="alumnium",
            run=alumnium.run,
            environment=null_environment,
            needs_browser=False,
            concurrency_safe=True,
            preflight=alumnium.preflight,
        ),
    }
    return Benchmark(
        name="WebVoyager",
        results_dir=config.RESULTS_DIR,
        load_tasks=tasks.load_tasks,
        load_refs=tasks.load_refs,
        bucket_of=tasks.bucket_of,
        runners=runners,
        judge=evaluate.JUDGE,
        default_runner="agent_browser",
        port_base=config.PORT_BASE,
    )
