"""Wire OSWorld onto core: computer-use runner + per-task Daytona desktop. The runner is
self_eval (it scores with OSWorld's official evaluators while the desktop is live and writes
eval.json), so core.run uses that verdict. Serial (provisioning is heavy).
"""
from benchmarks.osworld import config, evaluate, tasks
from benchmarks.osworld.env import kvm_vm, osworld_eval
from benchmarks.osworld.env.sandbox import osworld_environment, osworld_openbook_environment
from benchmarks.osworld.runners import agent_computer, gpt_astra, gpt_astra_openbook, verify_replan
from core.run import Benchmark, Runner


def _env_for_backend():
    """config.BACKEND == "kvm": the official VM on a user-provided Linux/KVM host (Task 8,
    env/kvm_vm.py). Otherwise unchanged: the existing per-task Daytona desktop. Only the Sonnet
    and Astra closed-book runners are wired to this -- the open-book and verify-replan runners
    keep osworld_environment/osworld_openbook_environment always, and refuse OSW_BACKEND=kvm in
    their own preflight instead of silently running on Daytona anyway (see gpt_astra_openbook.
    preflight and verify_replan.preflight)."""
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
        # Open-book Astra campaign (docs/g_astra_open_book_runner_implementation.md): same model,
        # own results tree, own environment (guest fixture proxy + egress lockdown before any
        # task config runs) -- never combined with the closed-book agent_computer_astra tree.
        config.ASTRA_OPENBOOK_SYSTEM_NAME: Runner(
            name=config.ASTRA_OPENBOOK_SYSTEM_NAME,
            run=gpt_astra_openbook.run,
            environment=osworld_openbook_environment,
            needs_browser=False,
            concurrency_safe=False,
            preflight=gpt_astra_openbook.preflight,
            self_eval=True,
        ),
        # Verify-Replan (docs/verify-replan-minimal-integration-plan.md): Execute -> Verify ->
        # [Done | Replan -> Execute recovery -> Verify], selected explicitly, never the default
        # -- the baseline runner above is unaffected by this existing at all.
        config.VR_SYSTEM_NAME: Runner(
            name=config.VR_SYSTEM_NAME,
            run=verify_replan.run,
            environment=osworld_environment,
            needs_browser=False,
            concurrency_safe=False,
            preflight=verify_replan.preflight,
            self_eval=True,
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
