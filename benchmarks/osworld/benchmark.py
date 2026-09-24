"""Wire OSWorld onto core: computer-use runner + per-task Daytona desktop. The runner is
self_eval (it scores with OSWorld's official evaluators while the desktop is live and writes
eval.json), so core.run uses that verdict. Serial (provisioning is heavy).
"""
from benchmarks.osworld import config, evaluate, tasks
from benchmarks.osworld.env import kvm_vm, osworld_eval
from benchmarks.osworld.env.sandbox import osworld_environment
from benchmarks.osworld.runners import agent_computer, gpt_astra
from core.run import Benchmark, Runner


def _env_for_backend():
    """config.BACKEND == "kvm": the official VM on a user-provided Linux/KVM host (Task 8,
    env/kvm_vm.py). Otherwise unchanged: the existing per-task Daytona desktop."""
    return kvm_vm.kvm_environment if config.BACKEND == "kvm" else osworld_environment


def _with_kvm_preflight(pf):
    """Under the kvm backend, run kvm_vm.preflight() (host capability: /dev/kvm, docker, the
    pinned qcow2) before the runner's own preflight (pinned-code check, official-protocol tool/
    leak preflight, etc) -- cheapest failure first, so a bad host is reported without ever
    reaching the runner-specific checks. With the daytona backend (today's default) this is a
    no-op passthrough: `pf` runs exactly as it did before this task existed."""
    if config.BACKEND != "kvm":
        return pf

    def combined():
        kvm_vm.preflight()
        if pf:
            pf()
    return combined


def build():
    # Named after the pinned model (config.SYSTEM_NAME), so each model's campaign gets its own
    # results tree and can never overwrite another's. Unpinned keeps the historical
    # "agent_computer" name -- that tree holds the mixed-model G3 runs, many without a saved
    # transcript, and has to stay exactly as it is.
    runners = {
        config.SYSTEM_NAME: Runner(
            name=config.SYSTEM_NAME,
            run=agent_computer.run,
            environment=_env_for_backend(),
            needs_browser=False,
            concurrency_safe=False,
            preflight=_with_kvm_preflight(agent_computer.preflight),
            self_eval=True,          # scores with OSWorld's own evaluators; writes eval.json
        ),
        # Independent model replication: GPT Astra via Codex CLI, with the same OSWorld MCP,
        # Daytona image and official evaluators as the Claude Code/Sonnet runner.
        config.ASTRA_SYSTEM_NAME: Runner(
            name=config.ASTRA_SYSTEM_NAME,
            run=gpt_astra.run,
            environment=_env_for_backend(),
            needs_browser=False,
            concurrency_safe=False,
            preflight=_with_kvm_preflight(gpt_astra.preflight),
            self_eval=True,
        ),
    }
    for name in runners:
        config.assert_new_infra_system(name)
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
