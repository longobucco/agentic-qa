"""Container entrypoint: run ONE WebArena task through ColorBrowserAgent, emit its native verdict.

ColorBrowserAgent (MadeAgents/browser-agent) is a self-contained BrowserGym WebArena harness with
its OWN deterministic eval — BrowserGym writes `summary_info.json` with `cum_reward` (>0 = pass),
the same WebArena evaluator our judge replicates. We run its `run_webarena.py` for one task
(pointed at our hosted stack via WA_* env), read cum_reward, and print marker lines the host
runner parses:
    VERDICT: SUCCESS|FAILURE
    RESULT_JSON: {"cum_reward": x, "verdict": ..., "model": ...}

Inputs (env): CBA_TASK (site group: reddit|shopping|shopping_admin|gitlab|map|cross),
CBA_TASK_ID (numeric), CBA_MODEL (OpenAI model), WA_<SITE> (hosted URLs), OPENAI_API_KEY/BASE_URL.
"""
import glob
import json
import os
import subprocess
import sys

REPO = "/app/browser-agent"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    site = os.environ.get("CBA_TASK", "").strip()
    task_id = os.environ.get("CBA_TASK_ID", "").strip()
    model = os.environ.get("CBA_MODEL", "gpt-5").strip()
    exp = "run"
    if not site or not task_id:
        print("VERDICT: FAILURE"); print('RESULT_JSON: {"error": "no task"}'); return 1

    cmd = ["python", "agent/run_webarena.py", "--task", site, "--task_ids", task_id,
           "--exp", exp, "--model_name", model, "--headless", "true"]
    log("running:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=int(os.environ.get("CBA_TIMEOUT", "900")))
        log((proc.stdout or "")[-3000:]); log((proc.stderr or "")[-2000:])
    except subprocess.TimeoutExpired:
        log("run_webarena timed out")

    # ColorBrowserAgent writes results/webarena/<group>/<exp>/webarena.<id>/.../summary_info.json
    cum = None
    for pat in (
        f"{REPO}/results/webarena/{site}/{exp}/webarena.{task_id}/**/summary_info.json",
        f"{REPO}/results/webarena/**/webarena.{task_id}/**/summary_info.json",
        f"{REPO}/results/**/webarena.{task_id}/**/summary_info.json",
    ):
        hits = glob.glob(pat, recursive=True)
        if hits:
            try:
                cum = json.loads(open(sorted(hits)[-1]).read()).get("cum_reward")
            except Exception as e:
                log("summary parse err", e)
            break

    verdict = "SUCCESS" if (cum is not None and float(cum) > 0.0) else "FAILURE"
    print("VERDICT:", verdict)
    print("RESULT_JSON:", json.dumps({"cum_reward": cum, "verdict": verdict, "model": model}))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
