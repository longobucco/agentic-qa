"""Wire OSWorld onto core: computer-use runner + per-task Daytona desktop. The runner is
self_eval (it scores with OSWorld's official evaluators while the desktop is live and writes
eval.json), so core.run uses that verdict. Serial (provisioning is heavy).
"""
from benchmarks.osworld import config, evaluate, tasks
from benchmarks.osworld.env.sandbox import osworld_environment
from benchmarks.osworld.runners import agent_computer
from core.run import Benchmark, Runner


def build():
    # Named after the pinned model (config.SYSTEM_NAME), so each model's campaign gets its own
    # results tree and can never overwrite another's. Unpinned keeps the historical
    # "agent_computer" name -- that tree holds the mixed-model G3 runs, many without a saved
    # transcript, and has to stay exactly as it is.
    runners = {
        config.SYSTEM_NAME: Runner(
            name=config.SYSTEM_NAME,
            run=agent_computer.run,
            environment=osworld_environment,
            needs_browser=False,
            concurrency_safe=False,
            self_eval=True,          # scores with OSWorld's own evaluators; writes eval.json
        ),
    }
    return Benchmark(
        name="OSWorld",
        results_dir=config.RESULTS_DIR,
        load_tasks=tasks.load_tasks,
        load_refs=tasks.load_refs,
        bucket_of=tasks.bucket_of,
        runners=runners,
        judge=evaluate.JUDGE,
        default_runner=config.SYSTEM_NAME,
    )
