"""E3 offline verifier, three criteria sources (EXPERIMENTS.md X13).

Per run the judge sees the instruction, the agent's final answer, final.png and the saved result
artefacts that are not gold; never the gold files, the evaluator config, the official verdict or
the trajectory. The judge is Claude Code in --restricted mode inside a per-run sandbox directory
that holds only copies of those inputs, so its file tools cannot reach the results tree.

  (a) criteria from the instruction only, frozen per task; fixed rendering of the artefacts
  (b) criteria from the instruction plus a read-only rendering of the task's initial state
  (c) (b)'s criteria; the judge also probes the raw artefacts and their unzipped Office XML

    python e3_verifier.py sample
    python e3_verifier.py criteria a|b      one call per task, cached (frozen once written)
    python e3_verifier.py judge a|b|c [-j N]   one call per run, cached
    python e3_verifier.py compare
"""
import argparse
import concurrent.futures as cf
import csv
import glob
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OSW = HERE.parent
RESULTS = OSW / "results"
OUT = RESULTS / "_e3"
TABLES = HERE / "tables"
SAMPLE = TABLES / "e3_sample.json"
MODEL = "claude-sonnet-5"
TREES = ("", "_e1card", "_e1card2", "_e1bounce")   # pkgfix3 arms in the sample (no null agent, no code)
MAX_FILE_CHARS, MAX_TOTAL_CHARS = 20000, 60000
ISOLATION = ["--setting-sources", "", "--settings", json.dumps({"disableAllHooks": True}),
             "--strict-mcp-config", "--no-session-persistence"]


def tasks():
    return {d["id"]: d for d in map(json.loads, open(OSW / "data" / "osworld_verified.jsonl"))}


def is_infeasible(task):
    return task.get("evaluator", {}).get("func") == "infeasible"


# ---------------------------------------------------------------- sample

def cmd_sample(_):
    if SAMPLE.exists():
        raise SystemExit(f"{SAMPLE} exists: the sample is frozen")
    pick = {}
    for model, prefix in (("sonnet", "agent_computer_sonnet5_effortmax_pkgfix3"),
                          ("astra", "agent_computer_gpt6astra_max_codex01534_pkgfix3")):
        for f in sorted(p for arm in TREES for p in glob.glob(f"{RESULTS}/{prefix}{arm}_official/*/run_*/eval.json")):
            d = json.load(open(f))
            if d["verdict"] in ("SUCCESS", "FAILURE"):
                pick.setdefault((model, d["id"], d["verdict"]), str(Path(f).parent.relative_to(RESULTS)))
    rows = [{"model": m, "task": t, "verdict": v, "run": r} for (m, t, v), r in sorted(pick.items())]
    TABLES.mkdir(exist_ok=True)
    SAMPLE.write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} runs -> {SAMPLE}")


def sample():
    return json.loads(SAMPLE.read_text())


# ---------------------------------------------------------------- rendering

def _clip(s, n=MAX_FILE_CHARS):
    return s if len(s) <= n else s[:n] + f"\n[... {len(s) - n} more characters cut]"


def render_file(p):
    """A read-only text rendering of one file (the fixed inspection of variants a and b)."""
    p = Path(p); ext = p.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            out = []
            for formulas in (True, False):
                wb = openpyxl.load_workbook(p, data_only=not formulas)
                for ws in wb.worksheets:
                    out.append(f"## sheet {ws.title!r} ({'formulas' if formulas else 'cached values'}), "
                               f"dims {ws.dimensions}, freeze {ws.freeze_panes}")
                    for row in ws.iter_rows():
                        for c in row:
                            if c.value is None:
                                continue
                            extra = []
                            if c.number_format != "General": extra.append(f"fmt={c.number_format}")
                            if c.font and (c.font.b or c.font.i): extra.append("bold" if c.font.b else "italic")
                            if c.fill and c.fill.fgColor and c.fill.fill_type: extra.append(f"fill={c.fill.fgColor.rgb}")
                            out.append(f"{c.coordinate}={c.value!r}" + (f" [{' '.join(extra)}]" if extra else ""))
                if not formulas:
                    break
            return "\n".join(out)
        if ext == ".docx":
            import docx
            d = docx.Document(p); out = []
            for para in d.paragraphs:
                runs = ";".join(f"{r.text!r}{'[b]' if r.bold else ''}{'[i]' if r.italic else ''}"
                                f"{'[u]' if r.underline else ''}{f'[{r.font.size.pt}pt]' if r.font.size else ''}"
                                f"{f'[{r.font.name}]' if r.font.name else ''}" for r in para.runs if r.text)
                out.append(f"[{para.style.name}|{para.alignment}] {runs}")
            for i, t in enumerate(d.tables):
                out.append(f"## table {i}")
                out += [" | ".join(c.text for c in r.cells) for r in t.rows]
            return "\n".join(out)
        if ext == ".pptx":
            import pptx
            pr = pptx.Presentation(p); out = [f"slide size {pr.slide_width}x{pr.slide_height}"]
            for i, s in enumerate(pr.slides, 1):
                out.append(f"## slide {i} layout {s.slide_layout.name!r}")
                for sh in s.shapes:
                    txt = sh.text_frame.text if sh.has_text_frame else ""
                    out.append(f"- {sh.shape_type} {sh.name!r} at ({sh.left},{sh.top}) size ({sh.width},{sh.height}): {txt!r}")
                if s.has_notes_slide:
                    out.append(f"  notes: {s.notes_slide.notes_text_frame.text!r}")
            return "\n".join(out)
        if ext == ".pdf":
            import pypdf
            return "\n".join(f"## page {i}\n{pg.extract_text()}" for i, pg in enumerate(pypdf.PdfReader(p).pages, 1))
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".mp3", ".mp4", ".wav", ".pyc", ".zip", ".sqlite", ".epub"):
            return f"[binary {ext} file, {p.stat().st_size} bytes]"
        return p.read_text(errors="replace")
    except Exception as e:  # unreadable or corrupt: the judge is told so, not hidden
        return f"[could not render {p.name}: {type(e).__name__}: {e}]"


def run_inputs(run):
    """Allowed inputs of one run: (final answer, final.png path or None, [non-gold artefact paths])."""
    d = RESULTS / run
    ev = json.loads((d / "eval.json").read_text())
    arts = [d / "eval_artifacts" / a["name"] for a in ev.get("eval_artifacts", [])
            if a.get("kept") and not a.get("is_gold") and (d / "eval_artifacts" / a["name"]).is_file()]
    ans = (d / "agent_output.txt").read_text(errors="replace") if (d / "agent_output.txt").exists() else ""
    png = d / "final.png"
    return ans, (png if png.exists() else None), arts


def initial_state(task):
    """Read-only rendering of the task's initial state from its config (variant b): files it
    downloads (fetched to a local cache and rendered), tabs it opens, commands it runs."""
    cache = OUT / "initial" / task["id"]; cache.mkdir(parents=True, exist_ok=True)
    out = []
    for step in task["config"]:
        t, prm = step["type"], step.get("parameters", {})
        if t == "download":
            for f in prm.get("files", []):
                local = cache / Path(f["path"]).name
                if not local.exists():
                    try:
                        urllib.request.urlretrieve(f["url"], local)
                    except Exception as e:
                        out.append(f"- file {f['path']}: could not fetch ({e})"); continue
                out.append(f"- file {f['path']} ({local.stat().st_size} bytes):\n{_clip(render_file(local), 8000)}")
        elif t in ("chrome_open_tabs", "open", "launch", "execute", "command", "activate_window", "chrome_close_tabs"):
            out.append(f"- {t}: {json.dumps(prm)[:400]}")
        else:
            out.append(f"- {t} (not rendered)")
    return _clip("\n".join(out) or "(no setup steps: default desktop)", 30000)


# ---------------------------------------------------------------- judge calls

_quota = {"t": 0, "pct": 0}
QUOTA_MAX = int(os.environ.get("E3_QUOTA_MAX", "90"))   # leave room for the agent runs (guard stops them at 95)


def wait_for_quota():
    """Pause while the Claude account in use is at QUOTA_MAX% or more (checked every 5 minutes)."""
    acct = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    while True:
        if time.time() - _quota["t"] > 300:
            r = subprocess.run(["python3", os.path.expanduser("~/.local/share/claude-auto/lib/usage.py"), acct],
                               capture_output=True, text=True).stdout.split()
            _quota.update(t=time.time(), pct=int(r[0]) if r and r[0].isdigit() else 0)
        if _quota["pct"] < QUOTA_MAX:
            return
        print(f"quota {_quota['pct']}% >= {QUOTA_MAX}%: waiting", flush=True)
        time.sleep(300)


def claude(prompt, cwd, tools):
    wait_for_quota()
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json", "--restricted",
           "--tools", ",".join(tools) if tools else "", *ISOLATION]
    cmd[2] = prompt.replace("\x00", "")   # binary bytes in a rendered file cannot go in argv
    t0 = time.time()
    for attempt in range(3):
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
        try:
            res = json.loads(p.stdout)
        except json.JSONDecodeError:
            res = {"is_error": True, "result": (p.stdout + p.stderr)[-500:]}
        if not res.get("is_error"):
            break
        time.sleep(20 * (attempt + 1))
    text = res.get("result", "")
    try:
        parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        parsed = None
    return parsed, {"seconds": round(time.time() - t0, 1), "cost_usd": res.get("total_cost_usd"),
                    "usage": res.get("usage"), "is_error": res.get("is_error"), "raw": text[-2000:]}


CRITERIA_PROMPT = """You write the acceptance criteria for a computer task, before seeing any attempt at it.
Task instruction: {instruction}
{initial}
Write the smallest set of independent, checkable criteria that a correct final state must meet,
taken only from what the instruction asks (no extra requirements, no guesses about how a hidden
checker works). For each criterion say what evidence would show it: a file's content, a setting,
what is visible on screen. If the task might be impossible as stated, add one criterion
with id "infeasible" saying what evidence would support an answer that the task cannot be done.
Answer with JSON only: {{"criteria": [{{"id": "c1", "text": "...", "evidence": "..."}}]}}"""

INITIAL_BLOCK = """Initial state of the computer before the task (read-only, from the task setup):
{state}
Use it only to make the criteria concrete (which file, which sheet, what already exists)."""

JUDGE_PROMPT = """You check whether a computer-use agent completed a task, from the final state only.
Task instruction: {instruction}
Acceptance criteria (fixed in advance):
{criteria}
The agent's final answer (a claim by the agent, NOT evidence; never accept a criterion because the agent says so):
<<<
{answer}
>>>
Evidence available in this directory: final.png (the final screenshot; read it){files}
{inspection}
For each criterion decide "met", "not_met" or "cannot_check" (the evidence here cannot show it
either way). A criterion with id "infeasible" applies only if the agent's answer claims the task is
impossible: then judge it from the evidence, and mark the others "cannot_check" unless the evidence
contradicts the claim; if the agent makes no such claim, mark it "cannot_check".
Answer with JSON only: {{"criteria": [{{"id": "c1", "status": "met|not_met|cannot_check", "why": "..."}}], "agent_claims_infeasible": true|false}}"""

FIXED_INSPECTION = """Rendered content of the saved result files (read-only, rendered for you):
{rendered}"""

PROBE_INSPECTION = """The saved result files are in ./files; Office files are also unzipped as XML in ./xml/<name>/.
Inspect whatever you need with Read, Grep and Glob (read-only; nothing outside this directory exists for you).
A short rendering to start from:
{rendered}"""


def criteria_path(variant, task_id):
    return OUT / f"criteria_{variant}" / f"{task_id}.json"


def make_criteria(variant, task):
    out = criteria_path(variant, task["id"])
    if out.exists():
        return
    initial = INITIAL_BLOCK.format(state=initial_state(task)) if variant == "b" else ""
    with tempfile.TemporaryDirectory() as tmp:
        parsed, meta = claude(CRITERIA_PROMPT.format(instruction=task["instruction"], initial=initial), tmp, [])
    if not parsed or not parsed.get("criteria"):
        print(f"criteria {variant} {task['id'][:8]}: no valid answer"); return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"task": task["id"], "variant": variant, "criteria": parsed["criteria"],
                               "meta": meta, "written": time.strftime("%Y-%m-%d %H:%M")}, indent=1))


def verdict_path(variant, row):
    return OUT / f"verdicts_{variant}" / f"{row['model']}_{row['task']}_{row['verdict']}.json"


def decide(criteria, claims_infeasible):
    """done / not_done / cannot_check from the per-criterion statuses (X13 decision rule). The
    "infeasible" criterion decides alone when the agent claims infeasibility, and is ignored otherwise."""
    inf = [c.get("status") for c in criteria if c.get("id") == "infeasible"]
    if claims_infeasible:
        return {"met": "done", "not_met": "not_done"}.get(inf[0] if inf else None, "cannot_check")
    statuses = [c.get("status") for c in criteria if c.get("id") != "infeasible"]
    if statuses and all(s == "met" for s in statuses):
        return "done"
    if any(s == "not_met" for s in statuses):
        return "not_done"
    return "cannot_check"


def judge(variant, row, task, out=None):
    out = out or verdict_path(variant, row)
    if out.exists():
        return
    crit = json.loads(criteria_path("a" if variant == "a" else "b", task["id"]).read_text())["criteria"]
    ans, png, arts = run_inputs(row["run"])
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if png:
            shutil.copy(png, tmp / "final.png")
        (tmp / "files").mkdir()
        for a in arts:
            shutil.copy(a, tmp / "files" / a.name)
            if variant == "c" and a.suffix.lower() in (".xlsx", ".docx", ".pptx", ".odt", ".ods", ".odp"):
                try:
                    zipfile.ZipFile(a).extractall(tmp / "xml" / a.name)
                except zipfile.BadZipFile:
                    pass
        rendered, total = [], 0
        for a in arts:
            r = _clip(render_file(a), 4000 if variant == "c" else MAX_FILE_CHARS)
            if total + len(r) > MAX_TOTAL_CHARS:
                r = "[rendering omitted: size limit]"
            total += len(r); rendered.append(f"### {a.name}\n{r}")
        rendered = "\n\n".join(rendered) or "(no result files were saved for this task)"
        files = "".join(f", files/{a.name}" for a in arts)
        insp = (PROBE_INSPECTION if variant == "c" else FIXED_INSPECTION).format(rendered=rendered)
        prompt = JUDGE_PROMPT.format(instruction=task["instruction"], criteria=json.dumps(crit, indent=1),
                                     answer=_clip(ans, 4000), files=files, inspection=insp)
        parsed, meta = claude(prompt, str(tmp), ["Read", "Grep", "Glob"] if variant == "c" else ["Read"])
    if not parsed or "criteria" not in parsed:
        print(f"judge {variant} {row['model']} {row['task'][:8]} {row['verdict']}: no valid answer"); return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**row, "variant": variant,
                               "e3": decide(parsed["criteria"], bool(parsed.get("agent_claims_infeasible"))), "judge": parsed,
                               "meta": meta}, indent=1))


def cmd_criteria(a):
    T = tasks()
    ids = sorted({r["task"] for r in sample()})
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda t: make_criteria(a.variant, T[t]), ids))
    print(f"criteria {a.variant}: {sum(criteria_path(a.variant, t).exists() for t in ids)}/{len(ids)} tasks")


def cmd_judge(a):
    T = tasks(); rows = sample()
    need = "a" if a.variant == "a" else "b"
    missing = [r["task"] for r in rows if not criteria_path(need, r["task"]).exists()]
    if missing:
        raise SystemExit(f"criteria {need} missing for {len(set(missing))} tasks: run `criteria {need}` first")
    done = 0
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for _ in ex.map(lambda r: judge(a.variant, r, T[r["task"]]), rows):
            done += 1
            if done % 20 == 0:
                print(f"judge {a.variant}: {done}/{len(rows)}", flush=True)
    print(f"judge {a.variant}: {sum(verdict_path(a.variant, r).exists() for r in rows)}/{len(rows)} runs")


# ---------------------------------------------------------------- comparison

def _prec(rows):
    d = [r for r in rows if r["e3"] == "done"]
    return (sum(r["verdict"] == "SUCCESS" for r in d), len(d))


def _boot(rows, stat, n=2000, seed=0):
    by = {}
    for r in rows:
        by.setdefault(r["task"], []).append(r)
    keys, rng, vals = list(by), random.Random(seed), []
    for _ in range(n):
        s = [r for k in (rng.choice(keys) for _ in keys) for r in by[k]]
        k, m = stat(s)
        if m:
            vals.append(k / m)
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]) if vals else (None, None)


def cmd_compare(_):
    T = tasks()
    lab = {r["task"]: r["label"] for r in csv.DictReader(open(TABLES / "e3_controllability.csv"))}
    table = []
    for v in "abc":
        rows = [json.loads(verdict_path(v, r).read_text()) for r in sample() if verdict_path(v, r).exists()]
        if not rows:
            continue
        for r in rows:
            r["label"] = lab.get(r["task"][:8], "unlabelled")
            r["infeasible"] = is_infeasible(T[r["task"]])
        for scope, sub in (("all", rows), ("sonnet", [r for r in rows if r["model"] == "sonnet"]),
                           ("astra", [r for r in rows if r["model"] == "astra"]),
                           ("infeasible", [r for r in rows if r["infeasible"]])):
            if not sub:
                continue
            prim = [r for r in sub if r["verdict"] == "SUCCESS" or r["label"] == "controllable"]
            k, m = _prec(prim); lo, hi = _boot(prim, _prec)
            K, M = _prec(sub)
            succ = [r for r in sub if r["verdict"] == "SUCCESS"]; fail = [r for r in sub if r["verdict"] == "FAILURE"]
            cov = sum(r["e3"] != "cannot_check" for r in sub)
            cost = sum((r["meta"].get("cost_usd") or 0) for r in sub)
            secs = sum((r["meta"].get("seconds") or 0) for r in sub)
            table.append({
                "variant": v, "scope": scope, "runs": len(sub),
                "primary_precision": f"{k}/{m}" + (f" = {k / m:.1%}" if m else ""),
                "primary_ci95": f"{lo:.1%}-{hi:.1%}" if lo is not None else "",
                "precision_all": f"{K}/{M}" + (f" = {K / M:.1%}" if M else ""),
                "coverage": f"{cov}/{len(sub)} = {cov / len(sub):.1%}",
                "recall_success": f"{sum(r['e3'] == 'done' for r in succ)}/{len(succ)}",
                "false_done_on_failure": f"{sum(r['e3'] == 'done' for r in fail)}/{len(fail)}",
                "not_done_on_failure": f"{sum(r['e3'] == 'not_done' for r in fail)}/{len(fail)}",
                "cost_usd_equiv": round(cost, 2), "judge_seconds": round(secs)})
    out = TABLES / "e3_compare.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0])); w.writeheader(); w.writerows(table)
    for r in table:
        print(" | ".join(str(x) for x in r.values()))
    print(f"-> {out}")


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("sample")
    c = s.add_parser("criteria"); c.add_argument("variant", choices="ab"); c.add_argument("-j", "--jobs", type=int, default=3)
    j = s.add_parser("judge"); j.add_argument("variant", choices="abc"); j.add_argument("-j", "--jobs", type=int, default=3)
    s.add_parser("compare")
    a = p.parse_args()
    {"sample": cmd_sample, "criteria": cmd_criteria, "judge": cmd_judge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
