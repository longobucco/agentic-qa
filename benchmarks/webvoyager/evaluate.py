"""WebVoyager-style judge, implemented with `claude -p` (multimodal) — no API key needed."""
import json
import re
import subprocess

from config import MODEL, JUDGE_TIMEOUT
from prompts import judge_prompt

JSON_RE = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}")


def _parse_verdict(text: str):
    matches = JSON_RE.findall(text or "")
    if matches:
        try:
            obj = json.loads(matches[-1])
            v = str(obj.get("verdict", "")).upper()
            return ("SUCCESS" if v.startswith("SUCC") else "FAILURE", obj.get("reason", ""))
        except Exception:
            pass
    up = (text or "").upper()
    if "SUCCESS" in up and "FAILURE" not in up:
        return ("SUCCESS", "parsed from text")
    return ("FAILURE", "could not parse verdict")


def judge(task, answer, ref, out):
    """Evaluate one task; writes out/eval.json and returns the verdict dict."""
    has_shots = any(out.glob("*.png"))
    cmd = [
        "claude", "-p", judge_prompt(task, answer, ref, out, has_screenshots=has_shots),
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", "12",
        "--add-dir", str(out),
    ]
    if MODEL:
        cmd += ["--model", MODEL]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=JUDGE_TIMEOUT)
        raw = proc.stdout
    except subprocess.TimeoutExpired:
        raw = ""

    text = raw
    try:
        text = json.loads(raw).get("result", raw)
    except Exception:
        pass

    verdict, reason = _parse_verdict(text)
    result = {"id": task["id"], "verdict": verdict, "reason": reason}
    (out / "eval.json").write_text(json.dumps(result, indent=2))
    return result
