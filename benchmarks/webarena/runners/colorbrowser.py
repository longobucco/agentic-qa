"""WebArena via ColorBrowserAgent (MadeAgents/browser-agent) — the third-party SOTA comparator.

ColorBrowserAgent topped WebArena (71.2%, Dec 2025): a BrowserGym harness with a dual-agent
Summarizer+Operator loop and progressive memory compression, run on GPT-5. Unlike agent_browser/
playwright_vision (our OWN Claude Code with different actuation), this is a DIFFERENT agent system
entirely — so `agent_browser` vs `colorbrowser` is "our pipeline vs a third-party SOTA agent."

It's a self-contained harness with its OWN deterministic eval (BrowserGym's `cum_reward`, the same
WebArena evaluator), so this runner is `self_eval=True`: we run it (in a container, pointed at our
hosted stack) and write eval.json from ITS verdict — `core.run` does NOT re-score it with our
judge. Both pipelines still end up on deterministic WebArena eval, so the A/B is apples-to-apples.

Model: GPT-5 (OpenAI) by default — a DIFFERENT model than our Claude pipeline, so this varies
harness AND model (ColorBrowserAgent is GPT-5-tuned; `--model_name` is configurable but its 71.2%
is GPT-5). Build the image: `docker build -f benchmarks/webarena/docker/Dockerfile.colorbrowser
-t colorbrowser:latest benchmarks/webarena/docker`.
"""
import json
import os
import subprocess
import uuid

from benchmarks.webarena import config, tasks
from core import results as results_io

# our WA_<SITE>_URL  ->  ColorBrowserAgent's WA_<SITE> env name (no _URL suffix)
_URL_MAP = {
    "WA_SHOPPING_URL": "WA_SHOPPING", "WA_SHOPPING_ADMIN_URL": "WA_SHOPPING_ADMIN",
    "WA_REDDIT_URL": "WA_REDDIT", "WA_GITLAB_URL": "WA_GITLAB", "WA_MAP_URL": "WA_MAP",
    "WA_WIKIPEDIA_URL": "WA_WIKIPEDIA", "WA_HOMEPAGE_URL": "WA_HOMEPAGE",
}


def _image():
    return os.environ.get("CBA_IMAGE", "colorbrowser:latest")


def _model():
    # GPT-5 by default — ColorBrowserAgent runs on OpenAI, NOT our Claude config.MODEL.
    return os.environ.get("CBA_MODEL", "gpt-5").strip()


def preflight():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY not set — ColorBrowserAgent runs on OpenAI (GPT-5 by default). "
            "Set OPENAI_API_KEY (+ OPENAI_BASE_URL for a compatible gateway), and CBA_MODEL to pick the model."
        )


def _site_and_id(task):
    sites = task.get("sites") or []
    site = sites[0] if len(sites) == 1 else "cross"   # ColorBrowserAgent groups multi-site as 'cross'
    return site, task["id"].split("-")[-1]


def _docker_cmd(task):
    site, tid = _site_and_id(task)
    cname = "cba_" + uuid.uuid4().hex[:12]
    args = [
        "docker", "run", "--rm", "--name", cname,
        "-e", "OPENAI_API_KEY", "-e", "OPENAI_BASE_URL",
        "-e", f"CBA_TASK={site}", "-e", f"CBA_TASK_ID={tid}",
        "-e", f"CBA_MODEL={_model()}", "-e", f"CBA_TIMEOUT={config.TASK_TIMEOUT}",
    ]
    for ours, theirs in _URL_MAP.items():
        if os.environ.get(ours):
            args += ["-e", f"{theirs}={os.environ[ours]}"]
    args.append(_image())
    return args, cname


def run(task, *, env, out, refs=None, dry=False):
    args, cname = _docker_cmd(task)
    if dry:
        print("DRY-RUN command:\n ", " ".join(repr(a) if " " in a else a for a in args))
        return None

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=config.TASK_TIMEOUT + 180)
        raw = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        raw = e.stdout if isinstance(e.stdout, str) else ""
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=30)

    verdict, result = "FAILURE", {}
    for line in (raw or "").splitlines():
        s = line.strip()
        if s.startswith("VERDICT:"):
            verdict = s[len("VERDICT:"):].strip() or "FAILURE"
        elif s.startswith("RESULT_JSON:"):
            try:
                result = json.loads(s[len("RESULT_JSON:"):].strip())
            except Exception:
                pass

    results_io.write_output(out, raw or "")
    results_io.write_result(out, {
        "id": task["id"],
        "bucket": tasks.bucket_of(task),
        "intent": task["intent"],
        "cum_reward": result.get("cum_reward"),
        "model": result.get("model", _model()),
    })
    # self_eval: write OUR eval.json from ColorBrowserAgent's own verdict (core skips the judge).
    results_io.write_eval(out, {
        "id": task["id"], "verdict": verdict,
        "reason": f"ColorBrowserAgent self-eval (cum_reward={result.get('cum_reward')})",
    })
    return ""
