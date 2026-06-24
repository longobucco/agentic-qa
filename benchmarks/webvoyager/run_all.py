"""Run the WebVoyager benchmark with the agent-browser + Claude Code harness.

Examples:
  python run_all.py --limit 3                       # first 3 tasks (smoke test)
  python run_all.py --per-site 3                    # stratified subset (3/site), serial
  python run_all.py --site Amazon Booking           # only these sites
  python run_all.py --ids Allrecipes--0             # specific task(s)
  python run_all.py                                 # full set (resumable)
  python run_all.py --no-eval                       # run agent only, judge later

NOTE on concurrency: agent-browser uses ONE shared daemon and `connect` only binds the
`default` session, so concurrency>1 cross-contaminates sessions. Keep --concurrency 1
locally. For real parallelism, isolate the daemon per worker (separate container/machine/
remote provider). The flag exists for those isolated setups.
"""
import argparse
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import RESULTS_DIR, PORT_BASE
from tasks import load_tasks, load_refs, filter_tasks
from agent import run_agent, task_dir
from evaluate import judge
import report

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def already_done(task):
    return (task_dir(task) / "eval.json").exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", nargs="*", help="filter by web_name (e.g. Amazon Booking)")
    ap.add_argument("--ids", nargs="*", help="explicit task ids")
    ap.add_argument("--per-site", type=int, help="cap tasks per site (stratified subset)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="agent-browser shares ONE daemon (default session only); >1 "
                         "cross-contaminates. Keep at 1 unless each worker has its own "
                         "daemon (separate container/machine/remote provider).")
    ap.add_argument("--no-eval", action="store_true", help="skip the judge step")
    ap.add_argument("--force", action="store_true", help="re-run completed tasks")
    args = ap.parse_args()

    tasks = filter_tasks(load_tasks(), site=args.site, ids=args.ids,
                         per_site=args.per_site, limit=args.limit, interleave=True)
    refs = load_refs()
    todo = [t for t in tasks if args.force or not already_done(t)]
    if args.concurrency > 1:
        log("WARNING: agent-browser uses ONE shared daemon; concurrency>1 will "
            "cross-contaminate sessions. Use 1 locally (see README). Continuing anyway.\n")
    log(f"Selected {len(tasks)} task(s); {len(todo)} to run "
        f"@ concurrency={args.concurrency}. Results -> {RESULTS_DIR/'agentbrowser'}\n")

    ports = queue.Queue()
    for i in range(max(1, args.concurrency)):
        ports.put(PORT_BASE + i)
    done = {"n": 0}

    def work(task):
        port = ports.get()
        t0 = time.time()
        try:
            answer, out = run_agent(task, port=port)
            verdict = ""
            if not args.no_eval:
                verdict = judge(task, answer, refs.get(task["id"]), out)["verdict"]
            return task["id"], verdict, answer, time.time() - t0, None
        except Exception as e:
            return task["id"], "", "", time.time() - t0, str(e)
        finally:
            ports.put(port)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futures = [ex.submit(work, t) for t in todo]
        for fut in as_completed(futures):
            tid, verdict, answer, dt, err = fut.result()
            done["n"] += 1
            tag = f"[{done['n']}/{len(todo)}]"
            if err:
                log(f"{tag} {tid}  ERROR: {err}")
            else:
                log(f"{tag} {tid}  {verdict or '(no eval)'}  "
                    f"({dt:.0f}s)  {answer[:70]!r}")

    log("")
    report.summarize("agentbrowser")


if __name__ == "__main__":
    main()
