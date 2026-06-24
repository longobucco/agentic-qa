"""Re-run WebVoyager through the ALUMNIUM pipeline (single OpenAI key).

Architecture (mirrors Alumnium's published 98.5% run, adapted to one key):
  - Orchestrator : Claude Code (`claude -p`) on your subscription           (no key)
  - Browser hands: Alumnium MCP (`uvx alumnium mcp`) -> Selenium Chrome,
                   internal do/check model = OpenAI                          (OPENAI_API_KEY)
  - Judge        : our subscription Claude judge (evaluate.py)               (no key)

Alumnium gives each `claude -p` its OWN MCP + Selenium Chrome (no shared daemon), so this
harness parallelizes locally. The OpenAI key is read from the environment and inherited by
the MCP subprocess (never written to disk). Set it before running:

  export OPENAI_API_KEY=sk-...     # or: set -a; . ./.env; set +a
  python alumnium_run.py --per-site 3 --concurrency 4
  python alumnium_run.py --ids Allrecipes--0 ArXiv--0
  python alumnium_run.py                 # full set (resumable)
  python alumnium_run.py --dry-run       # print the claude command, run nothing

Deviations from their exact run (document in the paper): OpenAI (not Azure) for the
do/check model; Claude (not GPT-5) as judge; no per-step screenshots (Alumnium MCP
exposes no screenshot tool), so the judge scores the answer vs the reference.
"""
import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import RESULTS_DIR, MODEL, MAX_TURNS, TASK_TIMEOUT
from tasks import load_tasks, load_refs, filter_tasks
from prompts import alumnium_prompt
from evaluate import judge
from agent import _extract_answer  # reuse the ANSWER parser
import report

SUBDIR = "alumnium"
ALUMNIUM_TOOLS = [
    "mcp__alumnium__start_driver", "mcp__alumnium__do", "mcp__alumnium__check",
    "mcp__alumnium__get", "mcp__alumnium__wait", "mcp__alumnium__stop_driver",
]
_print_lock = threading.Lock()


def mcp_config_file():
    """Write a temp MCP config (no secret; key is inherited from the environment)."""
    cfg = {"mcpServers": {"alumnium": {
        "type": "stdio", "command": "uvx", "args": ["alumnium", "mcp"],
    }}}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(cfg, f)
    f.close()
    return f.name


def run_one(task, refs, mcp_cfg, dry=False):
    out = RESULTS_DIR / SUBDIR / task["id"]
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "claude", "-p", alumnium_prompt(task),
        "--output-format", "json",
        "--mcp-config", mcp_cfg,
        "--allowedTools", *ALUMNIUM_TOOLS,
        "--dangerously-skip-permissions",
        "--max-turns", str(MAX_TURNS),
    ]
    if MODEL:
        cmd += ["--model", MODEL]
    if dry:
        print("DRY-RUN command:\n ", " ".join(repr(c) if " " in c else c for c in cmd))
        return None, out

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TASK_TIMEOUT)
    text = proc.stdout
    try:
        text = json.loads(proc.stdout).get("result", proc.stdout)
    except Exception:
        pass
    answer = _extract_answer(text)
    (out / "agent_output.txt").write_text(text or "")
    (out / "result.json").write_text(json.dumps(
        {"id": task["id"], "ques": task["ques"], "web": task.get("web"), "answer": answer},
        indent=2))
    return answer, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", nargs="*")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--per-site", type=int, help="cap tasks per site (stratified subset)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=1, help="parallel workers (start ~4-5)")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set. `export OPENAI_API_KEY=sk-...` first "
                         "(needed by the Alumnium MCP's do/check model).")

    tasks = filter_tasks(load_tasks(), site=args.site, ids=args.ids,
                         per_site=args.per_site, limit=args.limit, interleave=True)
    refs = load_refs()
    mcp_cfg = mcp_config_file()

    if args.dry_run:
        run_one(tasks[0], refs, mcp_cfg, dry=True)
        return

    todo = [t for t in tasks if args.force
            or not (RESULTS_DIR / SUBDIR / t["id"] / "eval.json").exists()]
    print(f"Alumnium pipeline: {len(tasks)} selected, {len(todo)} to run "
          f"@ concurrency={args.concurrency} -> {RESULTS_DIR/SUBDIR}\n", flush=True)
    done = {"n": 0}

    def work(t):
        t0 = time.time()
        try:
            answer, out = run_one(t, refs, mcp_cfg)
            verdict = "" if args.no_eval else judge(t, answer, refs.get(t["id"]), out)["verdict"]
            return t["id"], verdict, answer, time.time() - t0, None
        except Exception as e:
            return t["id"], "", "", time.time() - t0, str(e)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        for fut in as_completed([ex.submit(work, t) for t in todo]):
            tid, verdict, answer, dt, err = fut.result()
            done["n"] += 1
            with _print_lock:
                if err:
                    print(f"[{done['n']}/{len(todo)}] {tid}  ERROR: {err}", flush=True)
                else:
                    print(f"[{done['n']}/{len(todo)}] {tid}  {verdict or '(no eval)'}  "
                          f"({dt:.0f}s)  {answer[:70]!r}", flush=True)

    print()
    report.summarize(SUBDIR)


if __name__ == "__main__":
    main()
