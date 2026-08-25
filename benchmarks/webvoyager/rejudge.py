"""Re-judge trajectories whose eval.json failed to parse (the judge was rate-limited).

The agent run is fine — its answer + screenshots are saved — but a rate-limited judge can
return empty/garbled output that parses to "could not parse verdict" and is then miscounted
as a FAILURE. This re-runs ONLY the judge (no browser, no agent) on those saved trajectories
and rewrites eval.json, at low concurrency so it doesn't re-trigger the rate limit. Idempotent
and re-runnable: each pass targets only the verdicts that are still unparsed.

  python -m benchmarks.webvoyager.rejudge                 # re-judge all unparsed @ conc 3
  python -m benchmarks.webvoyager.rejudge --concurrency 2
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from benchmarks.webvoyager import config, evaluate, tasks
from core import results as results_io
from core.dotenv import load_dotenv

UNPARSED = "could not parse verdict"


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description="Re-judge unparsed WebVoyager trajectories")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--system", default="agent_browser")
    args = ap.parse_args(argv)

    tasks_by_id = {t["id"]: t for t in tasks.load_tasks()}
    refs = tasks.load_refs()
    base = config.RESULTS_DIR / args.system

    todo = []
    for ev in base.glob("*/run_1/eval.json"):
        rec = json.loads(ev.read_text())
        out = ev.parent
        if UNPARSED in rec.get("reason", "") and (out / "result.json").exists():
            todo.append(out)

    print(f"re-judging {len(todo)} unparsed trajectory(ies) @ concurrency {args.concurrency}\n")
    if not todo:
        return

    def work(out):
        tid = out.parent.name
        answer = json.loads((out / "result.json").read_text()).get("answer", "")
        verdict = evaluate.JUDGE(tasks_by_id[tid], answer, refs.get(tid), out)
        results_io.write_eval(out, {"id": tid, **verdict})
        return tid, verdict["verdict"], verdict["reason"]

    n = 0
    still = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        for fut in as_completed([ex.submit(work, o) for o in todo]):
            tid, v, r = fut.result()
            n += 1
            if r == UNPARSED:
                still += 1
            print(f"[{n}/{len(todo)}] {tid}: {v}" + (f"  ({r})" if v == "FAILURE" else ""))

    print(f"\ndone: {n} re-judged, {still} still unparsed (re-run this to retry them)")


if __name__ == "__main__":
    main()
