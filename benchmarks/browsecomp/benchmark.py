"""Wire BrowseComp onto `core`: tasks + runner + (text) judge + buckets -> one `Benchmark`.

Same shape as WebVoyager, with two seams swapped: the judge is the answer-vs-reference text
grader (not the screenshot judge), and the agent always runs in a per-task Steel container
(`core.steel`) so it's concurrency-safe out of the box.
"""
from benchmarks.browsecomp import config, evaluate, tasks
from benchmarks.browsecomp.runners import agent_browser, claude_code
from core.environment import null_environment
from core.run import Benchmark, Runner


def build():
    runners = {
        # Research via the containerized agent-browser harness (own Steel container per task).
        "agent_browser": Runner(
            name="agent_browser",
            run=agent_browser.run,
            environment=null_environment,
            needs_browser=False,
            concurrency_safe=True,
        ),
        # A/B comparator: PLAIN Claude Code (native WebSearch/WebFetch, NO browser harness) —
        # isolates "with vs without agent-browser," same model. `$0` (Claude subscription).
        "claude_code": Runner(
            name="claude_code",
            run=claude_code.run,
            environment=null_environment,
            needs_browser=False,
            concurrency_safe=True,
        ),
    }
    return Benchmark(
        name="BrowseComp",
        results_dir=config.RESULTS_DIR,
        load_tasks=tasks.load_tasks,
        load_refs=tasks.load_refs,
        bucket_of=tasks.bucket_of,
        runners=runners,
        judge=evaluate.JUDGE,
        default_runner="agent_browser",
    )
