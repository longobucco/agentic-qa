"""Shared orchestration loop for every benchmark.

    filter -> (per task) provision env -> run agent xN -> judge (xM if nondeterministic)
    -> aggregate -> report

A benchmark hands us a `Benchmark` describing its tasks, runners (systems-under-test),
judge, and buckets; the entry point is then a one-liner:

    from core.run import main
    from benchmarks.webvoyager.benchmark import build
    main(build())

`--runs N` makes N-runs-per-task first-class (agent re-rolls), and `--judge-repeats M`
re-judges each trajectory for judge variance — but only if the judge is nondeterministic;
a deterministic checker (WebArena) is always called exactly once regardless of M.

CONCURRENCY: agent-browser uses ONE shared daemon, so its runner is not concurrency-safe;
`--concurrency >1` warns unless the selected runner declares otherwise (e.g. Alumnium, which
gives each worker its own MCP + browser).
"""
import argparse
import json
import os
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from core import procgroups
from core import results as results_io
from core import reporting
from core.agent_loop import preview
from core.dotenv import load_dotenv
from core.environment import Env, null_environment
from core.tasks import default_bucket, filter_tasks

_print_lock = threading.Lock()

# Curated pinning knobs recorded per run so a harness A/B is auditable: the comparison is only
# "harness, not model" if the model + step/budget knobs match across the two variants.
_HARNESS_ENV_KEYS = (
    "WV_MODEL", "WV_MAX_TURNS", "WA_MAX_TURNS", "WA_TASK_TIMEOUT",
    "BC_MAX_TURNS", "BC_TASK_TIMEOUT", "BU_PROVIDER", "BU_MODEL", "BU_IMAGE",
    # OSWorld pins the release, step budget, timeout and guest image so a desktop A/B is auditable.
    "OSW_RELEASE", "OSW_MAX_STEPS", "OSW_TASK_TIMEOUT", "OSW_IMAGE",
    "OSW_ASTRA_MODEL", "OSW_ASTRA_REASONING_EFFORT", "OSW_ASTRA_CODEX_VERSION",
    # Protocol knobs.
    "OSW_EFFORT", "OSW_MAX_OUTPUT_TOKENS", "OSW_POPULATION", "OSW_SCREEN_WIDTH",
    "OSW_SCREEN_HEIGHT",
    # Official-fidelity knobs.
    "OSW_PROTOCOL", "OSW_BACKEND", "OSW_KVM_IMAGE", "OSW_KVM_QCOW2_SHA256",
    "OSW_SLEEP_AFTER_EXECUTION", "OSW_POST_SETUP_WAIT_S", "OSW_PRE_EVAL_WAIT_S",
    "OSW_KVM_CLIENT_PASSWORD", "OSW_CLAUDE_CODE_VERSION",
)


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def _write_harness_record(benchmark, runner, args):
    """Persist the pinned config for this system so `reporting.ab_compare` can audit that two
    harness variants ran the SAME model/budget (else the delta is confounded)."""
    rec = {
        "system": runner.name,
        "runs": args.runs,
        "concurrency": args.concurrency,
        "judge_repeats": args.judge_repeats,
        "env": {k: os.environ[k] for k in _HARNESS_ENV_KEYS if os.environ.get(k)},
    }
    d = benchmark.results_dir / runner.name
    d.mkdir(parents=True, exist_ok=True)
    (d / "harness.json").write_text(json.dumps(rec, indent=2))


@dataclass
class Runner:
    """A system-under-test. `run(task, *, env, out, refs, dry=False) -> answer`.

    The runner does its own work into `out` (result.json, transcript, screenshots) and
    returns the answer string; `core.run` owns judging and eval.json.
    """
    name: str
    run: Callable[..., str]
    environment: Callable[..., object] = null_environment   # (task, *, port) -> CM
    needs_browser: bool = False                             # allocate a CDP port?
    concurrency_safe: bool = False                          # ok at --concurrency > 1?
    preflight: Callable[[], None] = None                   # raise SystemExit if unmet
    self_eval: bool = False                                # runner is a self-contained harness
                                                            # that writes its OWN eval.json (e.g.
                                                            # ColorBrowserAgent's cum_reward) ->
                                                            # core skips the benchmark judge


@dataclass
class Benchmark:
    name: str
    results_dir: object
    load_tasks: Callable[[], list]
    runners: dict                                  # system name -> Runner
    judge: object                                  # core.judge.Judge
    bucket_of: Callable[[dict], str] = default_bucket
    load_refs: Callable[[], dict] = dict
    default_runner: str = None
    port_base: int = 9222


def _parser(benchmark):
    systems = list(benchmark.runners)
    ap = argparse.ArgumentParser(description=f"{benchmark.name} harness")
    ap.add_argument("--system", choices=systems,
                    default=benchmark.default_runner or systems[0],
                    help="system-under-test to run")
    ap.add_argument("--bucket", "--site", nargs="*", dest="buckets",
                    help="filter by bucket (e.g. site / web_name / topic)")
    ap.add_argument("--ids", nargs="*", help="explicit task ids")
    ap.add_argument("--per-bucket", "--per-site", type=int, dest="per_bucket",
                    help="cap tasks per bucket (stratified subset)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--runs", type=int, default=1,
                    help="agent re-rolls per task (mean±std across runs)")
    ap.add_argument("--judge-repeats", type=int, default=1, dest="judge_repeats",
                    help="re-judge each trajectory N times (LLM judges only)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--no-eval", action="store_true", help="skip the judge step")
    ap.add_argument("--force", action="store_true", help="re-run completed runs")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command for the first task, run nothing")
    return ap


def _install_signal_handlers():
    """Install SIGTERM/SIGINT handlers that reap every registered agent CLI process group
    (`core.procgroups`) before the interrupt propagates. A benchmark run's agent spawners
    (`core.agent_loop._run_raw`, `core.codex_loop.run_codex_meta`) already killpg their own
    child on THEIR OWN timeout; this covers the harness process itself being torn down
    (campaign driver SIGTERM, Ctrl-C, or an unhandled exception unwinding `main`) -- without
    it, the agent CLI (+ its MCP-server grandchild) is orphaned with no timeout, spending
    subscription quota against a VM that's being removed underneath it.

    Only installed in the main thread: `signal.signal` raises ValueError from any other thread
    (e.g. a `--concurrency` worker), and the benchmark's own worker threads never need it --
    they don't own the process's signal disposition. With no handler installed, nothing about
    a run changes (no groups are ever left un-reaped that wouldn't be anyway).

    Each handler calls `procgroups.mark_interrupted()` BEFORE `kill_all()` -- units run inside
    a `ThreadPoolExecutor` (`_main`'s work()), so a worker thread whose agent-CLI group is
    killed by this handler must be able to tell that apart from its own ordinary timeout and
    raise `procgroups.Interrupted` (core.agent_loop._run_raw / core.codex_loop.run_codex_meta
    both check the flag) rather than quietly returning partial output that `_main`'s
    as_completed loop would otherwise judge and score as if the run had actually finished.
    `_main`'s own as_completed loop is responsible for turning that into
    `ex.shutdown(wait=True, cancel_futures=True)` -- this function only sets the flag and kills
    the groups; it never touches the executor.

    SIGTERM is converted into `SystemExit(128+signum)` rather than `os._exit`, so `_main`'s own
    `except BaseException` around its as_completed loop can catch it and call
    `ex.shutdown(wait=True, cancel_futures=True)`. The `finally` blocks in environment context
    managers (e.g. stopping/removing a KVM container) do NOT run here on the main thread --
    they run on the WORKER thread, as `procgroups.Interrupted` unwinds through `work()`'s
    `with runner.environment(...) as env:` block; `wait=True` is what makes the main thread
    block long enough for that teardown to actually finish before the process exits. SIGINT
    (Ctrl-C) kills the groups first and then raises KeyboardInterrupt as usual, so existing
    Ctrl-C behavior is unchanged apart from the reap (and the same executor-shutdown handling
    in `_main`).
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_sigterm(signum, frame):
        procgroups.mark_interrupted()
        procgroups.kill_all()
        raise SystemExit(128 + signum)

    def _on_sigint(signum, frame):
        procgroups.mark_interrupted()
        procgroups.kill_all()
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigint)


def main(benchmark, argv=None):
    load_dotenv()   # repo-root .env -> every benchmark run shares DAYTONA/OPENAI/... keys
    _install_signal_handlers()
    try:
        _main(benchmark, argv)
    finally:
        # Belt-and-braces: an unhandled exception unwinding this frame must not leave an agent
        # CLI's process group running past the VM/container it was driving being torn down.
        procgroups.kill_all()


def _main(benchmark, argv=None):
    args = _parser(benchmark).parse_args(argv)
    runner = benchmark.runners[args.system]
    judge = benchmark.judge
    need_eval = not args.no_eval

    if runner.preflight and not args.dry_run:
        runner.preflight()

    tasks = filter_tasks(
        benchmark.load_tasks(), bucket_of=benchmark.bucket_of, buckets=args.buckets,
        ids=args.ids, per_bucket=args.per_bucket, limit=args.limit, interleave=True,
    )
    refs = benchmark.load_refs()

    if args.dry_run:
        if not tasks:
            log("No tasks selected.")
            return
        out = results_io.run_dir(benchmark.results_dir, runner.name, tasks[0]["id"], 1)
        runner.run(tasks[0], env=Env(port=benchmark.port_base), out=out, refs=refs, dry=True)
        return

    # Build the work list: each (task, run_idx) not already done (unless --force).
    units = []
    for t in tasks:
        for k in range(1, args.runs + 1):
            out = results_io.run_dir(benchmark.results_dir, runner.name, t["id"], k)
            if args.force or not results_io.is_done(out, need_eval=need_eval):
                units.append((t, k))

    _write_harness_record(benchmark, runner, args)   # pinned-config audit trail for A/B

    conc = max(1, args.concurrency)
    if conc > 1 and not runner.concurrency_safe:
        log(f"WARNING: runner '{runner.name}' is not concurrency-safe (agent-browser shares "
            f"ONE daemon; >1 cross-contaminates sessions). Keep --concurrency 1 unless each "
            f"worker has its own daemon. Continuing anyway.\n")
    judge_reps = 1 if (judge.is_deterministic or not need_eval) else max(1, args.judge_repeats)

    log(f"{benchmark.name} [{runner.name}]: {len(tasks)} task(s) x {args.runs} run(s); "
        f"{len(units)} to run @ concurrency={conc}"
        f"{'' if judge_reps == 1 else f', judge x{judge_reps}'}. "
        f"Results -> {benchmark.results_dir / runner.name}\n")
    if not units:
        reporting.summarize(benchmark.results_dir, runner.name, title=benchmark.name)
        return

    ports = queue.Queue()
    for i in range(conc):
        ports.put(benchmark.port_base + i)

    def work(unit):
        task, k = unit
        out = results_io.run_dir(benchmark.results_dir, runner.name, task["id"], k)
        t0 = time.time()
        if procgroups.is_interrupted():
            # The harness is already shutting down (SIGTERM/SIGINT) -- never start a unit once
            # that's true: no env provisioning (e.g. no new KVM container), no agent spawn.
            # Recorded exactly like a unit that WAS interrupted mid-flight (below) so a resumed
            # campaign retries it rather than treating "never even started" as a silent gap.
            results_io.write_infra_error(out, {
                "id": task["id"], "run": k, "outcome": "INTERRUPTED",
                "error_type": "Interrupted",
                "error": "harness interrupted before this unit started",
                "elapsed_s": 0.0, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            return task["id"], k, None, "", 0.0, "interrupted"
        out.mkdir(parents=True, exist_ok=True)
        port = ports.get() if runner.needs_browser else None
        try:
            with runner.environment(task, port=port) as env:
                if procgroups.is_interrupted():
                    # Provisioning (e.g. a KVM container) may itself have been slow enough for
                    # the interrupt to land while it ran, uninterruptible in its own right --
                    # catch that here, before the runner is ever called, so teardown starts at
                    # once instead of waiting for the agent CLI to be spawned and then notice.
                    raise procgroups.Interrupted(
                        "harness interrupted after environment provisioning, "
                        "before the agent call")
                answer = runner.run(task, env=env, out=out, refs=refs)
            verdict = None
            if need_eval and runner.self_eval:
                # self-contained harness: the runner wrote its OWN eval.json (its native
                # deterministic eval). Don't run the benchmark judge; just surface its verdict.
                ev = out / "eval.json"
                verdict = json.loads(ev.read_text()).get("verdict") if ev.exists() else None
            elif need_eval:
                ref = refs.get(task["id"])
                verdicts = [judge(task, answer, ref, out) for _ in range(judge_reps)]
                rec = results_io.aggregate_judge(verdicts)
                results_io.write_eval(out, {"id": task["id"], **rec})
                verdict = rec["verdict"]
            return task["id"], k, verdict, answer, time.time() - t0, None
        except procgroups.Interrupted as e:
            # The agent CLI's process group was reaped because the HARNESS was interrupted, not
            # because of a normal timeout -- never write eval.json for this: it wasn't scored,
            # it was killed. write_infra_error keeps is_done() false so a resumed campaign
            # retries it. The environment context manager's own `finally` (e.g. stopping a KVM
            # container) has already run by the time this except clause is reached.
            results_io.write_infra_error(out, {
                "id": task["id"], "run": k, "outcome": "INTERRUPTED",
                "error_type": type(e).__name__, "error": str(e),
                "elapsed_s": round(time.time() - t0, 1),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            return task["id"], k, None, "", time.time() - t0, str(e)
        except Exception as e:
            # never reached a verdict (provisioning/controller/harness) -- persist it so infra
            # flakiness is measurable instead of scrolling past in stdout
            results_io.write_infra_error(out, {
                "id": task["id"], "run": k,
                "outcome": "HARNESS_ERROR" if isinstance(e, (TypeError, AttributeError, KeyError,
                                                            ImportError, NameError))
                            else "INFRA_FLAKE",
                "error_type": type(e).__name__, "error": str(e),
                "elapsed_s": round(time.time() - t0, 1),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            return task["id"], k, None, "", time.time() - t0, str(e)
        finally:
            if port is not None:
                ports.put(port)

    done = {"n": 0}
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futures = [ex.submit(work, u) for u in units]
        try:
            for fut in as_completed(futures):
                tid, k, verdict, answer, dt, err = fut.result()
                done["n"] += 1
                tag = f"[{done['n']}/{len(units)}]"
                run_tag = f"{tid}#{k}" if args.runs > 1 else tid
                if err:
                    log(f"{tag} {run_tag}  ERROR: {err}")
                else:
                    log(f"{tag} {run_tag}  {verdict or '(no eval)'}  "
                        f"({dt:.0f}s)  {answer[:70]!r}")
        except BaseException:
            # SIGTERM/SIGINT (raised into whichever `as_completed` wait this loop was blocked
            # in) or any other unexpected exception: stop waiting on the queue and cancel every
            # unit that hasn't started yet (cancel_futures=True) -- otherwise this `with`
            # block's own __exit__ would call the executor's default shutdown(wait=True),
            # which drains the ENTIRE remaining queue instead of stopping. wait=True still
            # blocks for any unit already in flight, whose own agent-CLI group was just killed
            # by the signal handler (or which observes procgroups.is_interrupted() itself) --
            # it raises procgroups.Interrupted, and its environment's `finally` (e.g. stopping a
            # KVM container) runs as that exception unwinds, before this call returns.
            ex.shutdown(wait=True, cancel_futures=True)
            raise

    log("")
    reporting.summarize(benchmark.results_dir, runner.name, title=benchmark.name)


if __name__ == "__main__":
    import sys as _sys
    _sys.exit("core.run has no benchmark of its own to run -- invoke the benchmark's own entry "
              "point instead, e.g. `python -m benchmarks.osworld.run` (which imports and calls "
              "core.run.main(build())). Running `python -m core.run` directly imports this "
              "module without ever calling main(), so it silently does nothing.")
