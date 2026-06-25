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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

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


def main(benchmark, argv=None):
    load_dotenv()   # repo-root .env -> every benchmark run shares DAYTONA/OPENAI/... keys
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
        out.mkdir(parents=True, exist_ok=True)
        port = ports.get() if runner.needs_browser else None
        t0 = time.time()
        try:
            with runner.environment(task, port=port) as env:
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
        except Exception as e:
            return task["id"], k, None, "", time.time() - t0, str(e)
        finally:
            if port is not None:
                ports.put(port)

    done = {"n": 0}
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for fut in as_completed([ex.submit(work, u) for u in units]):
            tid, k, verdict, answer, dt, err = fut.result()
            done["n"] += 1
            tag = f"[{done['n']}/{len(units)}]"
            run_tag = f"{tid}#{k}" if args.runs > 1 else tid
            if err:
                log(f"{tag} {run_tag}  ERROR: {err}")
            else:
                log(f"{tag} {run_tag}  {verdict or '(no eval)'}  "
                    f"({dt:.0f}s)  {answer[:70]!r}")

    log("")
    reporting.summarize(benchmark.results_dir, runner.name, title=benchmark.name)
