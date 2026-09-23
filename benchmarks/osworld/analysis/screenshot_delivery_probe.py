"""Does each agent CLI deliver a 1920x1080 MCP screenshot to the model at full resolution?
A downscaled image turns every coordinate the model reads into a systematic offset. Local only:
stub MCP server, no sandbox. Uses the benchmark's own model pins and CLI isolation flags.

    python -m benchmarks.osworld.analysis.screenshot_delivery_probe --cli claude --trials 5
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

from benchmarks.osworld import config
from core.agent_loop import build_claude_cmd, run_claude_meta
from core.codex_loop import APPROVAL_MODE, DISABLED_FEATURES

W, H = 1920, 1080
PROMPT = ("Call the screenshot tool once. The image shows one red dot. Call report(x, y) with the "
          "pixel coordinates of the dot's center in the screenshot's own pixel space. Then stop.")
REPO = str(Path(__file__).resolve().parents[3])


def verdict(points, tol_px=15):
    errs = [((tx - ax) ** 2 + (ty - ay) ** 2) ** 0.5 for (tx, ty), (ax, ay) in points]
    rx = statistics.median(ax / tx for (tx, _), (ax, _) in points if tx)
    ry = statistics.median(ay / ty for (_, ty), (_, ay) in points if ty)
    mean = statistics.mean(errs)
    return {"mean_error_px": round(mean, 1), "ratio_x": round(rx, 3), "ratio_y": round(ry, 3),
            "ok": mean <= tol_px and abs(rx - 1) < 0.03 and abs(ry - 1) < 0.03}


def _env(x, y, out):
    return {"OSW_PROBE_X": str(x), "OSW_PROBE_Y": str(y), "OSW_PROBE_OUT": out,
            "PYTHONPATH": REPO}


def _run_claude(x, y, out):
    cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mcpServers": {"osworld": {"command": sys.executable,
               "args": ["-m", "benchmarks.osworld.mcp.probe_server"], "env": _env(x, y, out)}}}, cfg)
    cfg.close()
    cmd = build_claude_cmd(PROMPT, model=config.MODEL or "claude-sonnet-5", max_turns=6,
                           mcp_config=cfg.name,
                           allowed_tools=["mcp__osworld__screenshot", "mcp__osworld__report"],
                           effort=config.EFFORT or None)
    run_claude_meta(cmd, timeout=600)


def _run_codex(x, y, out):
    import subprocess
    env = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in _env(x, y, out).items()) + " }"
    cmd = ["codex", "exec", "--json", "--color", "never", "--model", config.ASTRA_MODEL,
           f"--{APPROVAL_MODE}", "--skip-git-repo-check", "--ignore-user-config",
           "--ignore-rules"]
    for feature in DISABLED_FEATURES:
        cmd += ["--disable", feature]
    cmd += ["--cd", tempfile.mkdtemp(),
            "-c", f"mcp_servers.osworld.command={json.dumps(sys.executable)}",
            "-c", "mcp_servers.osworld.args=[\"-m\",\"benchmarks.osworld.mcp.probe_server\"]",
            "-c", f"mcp_servers.osworld.env={env}",
            "-c", f"model_reasoning_effort={json.dumps(config.ASTRA_REASONING_EFFORT)}", PROMPT]
    subprocess.run(cmd, capture_output=True, timeout=600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", choices=["claude", "codex"], required=True)
    ap.add_argument("--trials", type=int, default=5)
    args = ap.parse_args()
    rng = random.Random(0)
    points = []
    for _ in range(args.trials):
        x, y = rng.randrange(60, W - 60), rng.randrange(60, H - 60)
        out = tempfile.mktemp(suffix=".json")
        (_run_claude if args.cli == "claude" else _run_codex)(x, y, out)
        try:
            ans = json.loads(Path(out).read_text())
            points.append(((x, y), (ans["x"], ans["y"])))
        except (OSError, ValueError, KeyError):
            print(f"trial at ({x}, {y}): no report() call recorded", file=sys.stderr)
    if not points:
        raise SystemExit("no trial produced an answer -- probe inconclusive")
    rec = {"cli": args.cli, "trials": args.trials, "answered": len(points),
           "points": points, **verdict(points)}
    dest = config.RESULTS_DIR / "_probes" / f"screenshot_delivery_{args.cli}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rec, indent=2))
    print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
