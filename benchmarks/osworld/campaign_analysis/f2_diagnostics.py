"""F2 (EXPERIMENTS.md X15): zero-cost diagnostics over every archived run.

    .venv/bin/python benchmarks/osworld/campaign_analysis/f2_diagnostics.py [--images] [--out tables]

Two measures, both from artefacts already on disk:

1. **End-of-run class**, which X16's addendum 1 says the false-DONE counts need: `answer` alone
   overstates false *claims* of success, because a run that exhausted its budget and says so in its
   final report still has `answer: DONE`. Classes: claims_done / step_cap_admitted / declares_fail /
   cli_aborted / no_answer.

2. **No-op repetition** (Mis-Score's largest failure category): a `computer` call whose screen is
   unchanged from the previous call, and the stricter case of the *same* call repeated with no
   change. Needs the screenshots, which are in conversation.jsonl as base64 PNG, so it only runs
   with --images and is cached in the output CSV.

A run counts iff eval.json carries a verdict (core.results.is_done). infra_error.json is an
append-only log of attempts that died before a verdict, so a run holding both is a retry that
succeeded and counts -- getting this wrong dropped 339 judged runs on the first pass (12/11).
"""
import argparse
import base64
import csv
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"

GRID, TOL = 16, 3.0
# Actions that are not expected to change the screen, so an unchanged screen after one is not a
# no-op. `wait` is the agent deliberately pausing; zoom/screenshot/mouse_move only observe.
OBSERVE_ONLY = {"screenshot", "wait", "zoom", "mouse_move", "cursor_position"}

# The agent admitting it ran out of budget, in its own final report. Upstream has no field for
# this: the answer stays DONE, so the admission has to be read from the text.
STEP_CAP = re.compile(
    r"maximum (number of )?steps|step limit|reached the (maximum|step)|ran out of steps|"
    r"out of steps|no (further|more) steps|step budget|incomplete due to", re.I)


def end_of_run_class(run_dir, result):
    answer = (result.get("answer") or "").strip().upper()
    report = ""
    out = run_dir / "agent_output.txt"
    if out.exists():
        try:
            report = out.read_text(errors="replace")
        except OSError:
            report = ""
    if result.get("agent_api_error_status") or result.get("agent_stop_reason") in ("error",
                                                                                   "timeout"):
        return "cli_aborted"
    if answer == "FAIL":
        return "declares_fail"
    if answer == "DONE":
        return "step_cap_admitted" if STEP_CAP.search(report) else "claims_done"
    return "no_answer"


def _blockmeans(png):
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("L").resize((GRID, GRID), Image.Resampling.BOX)
    return list(im.getdata())


def noop_counts(run_dir):
    """(comparisons, screen-unchanged, identical repeats, identical repeats with no change)."""
    tr = run_dir / "conversation.jsonl"
    if not tr.exists():
        return None
    calls = []
    for line in tr.open(errors="replace"):
        if '"mcp_tool_call"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        it = ev.get("item") or {}
        if ev.get("type") != "item.completed" or it.get("type") != "mcp_tool_call":
            continue
        if it.get("tool") != "computer":
            continue
        args = it.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        img = None
        for part in ((it.get("result") or {}).get("content") or []):
            if isinstance(part, dict) and part.get("type") == "image" and part.get("data"):
                try:
                    img = _blockmeans(base64.b64decode(part["data"]))
                except Exception:
                    img = None
                break
        calls.append((json.dumps(args, sort_keys=True), img, args.get("action")))
    if len(calls) < 2:
        return None
    cmp_ = unchanged = repeats = repeat_noop = 0
    for i in range(1, len(calls)):
        a_prev, img_prev, _ = calls[i - 1]
        a_cur, img_cur, action = calls[i]
        if img_prev is None or img_cur is None:
            continue
        cmp_ += 1
        same_screen = max(abs(x - y) for x, y in zip(img_prev, img_cur)) <= TOL
        if action in OBSERVE_ONLY:
            continue
        if same_screen:
            unchanged += 1
        if a_cur == a_prev:
            repeats += 1
            if same_screen:
                repeat_noop += 1
    return cmp_, unchanged, repeats, repeat_noop


def load(images=False):
    sets = json.loads(SETS.read_text())["sets"]
    setof = {t: n for n, s in sets.items() if n != "population361" for t in s["tasks"]}
    rows = []
    for p in sorted(RESULTS.glob("agent_computer_*_official/*/run_*/result.json")):
        ev = p.parent / "eval.json"
        if not ev.exists():
            continue
        try:
            r = json.loads(p.read_text())
            verdict = json.loads(ev.read_text()).get("verdict")
        except ValueError:
            continue
        if verdict not in ("SUCCESS", "FAILURE"):
            continue
        tree = p.parents[2].name
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else "null")
        if model == "null":
            continue
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("agent_computer_gpt6astra_max_codex01534_protocol361img3", "")
                   .replace("_official", "").strip("_") or "a0")
        row = {"model": model, "arm": arm, "tree": tree, "task": p.parents[1].name,
               "set": setof.get(p.parents[1].name, "?"), "verdict": verdict,
               "steps": r.get("agent_steps_used"),
               "class": end_of_run_class(p.parent, r)}
        if images:
            n = noop_counts(p.parent)
            row.update(dict(zip(("cmp", "unchanged", "repeats", "repeat_noop"),
                                n if n else (None, None, None, None))))
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", action="store_true",
                    help="also run the no-op detector (decodes every screenshot: slow)")
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rows = load(a.images)
    print(f"F2 — {len(rows)} judged runs\n")

    print("End-of-run class, by model and arm (share of FAILED runs):")
    hdr = (f"{'model':7} {'arm':14} {'failed':>7} {'claims_done':>12} {'step_cap':>9} "
           f"{'declares_fail':>14} {'cli_aborted':>12}")
    print(hdr); print("-" * len(hdr))
    for key in sorted({(r["model"], r["arm"]) for r in rows}):
        f = [r for r in rows if (r["model"], r["arm"]) == key and r["verdict"] == "FAILURE"]
        if len(f) < 5:
            continue
        c = Counter(r["class"] for r in f)
        print(f"{key[0]:7} {key[1]:14} {len(f):>7} {c['claims_done']:>12} "
              f"{c['step_cap_admitted']:>9} {c['declares_fail']:>14} {c['cli_aborted']:>12}")

    print("\nThe X16 correction this measures: a DONE that admits the step cap is not a false "
          "claim of success.")
    for model in ("astra", "sonnet"):
        f = [r for r in rows if r["model"] == model and r["verdict"] == "FAILURE"]
        if not f:
            continue
        c = Counter(r["class"] for r in f)
        done = c["claims_done"] + c["step_cap_admitted"]
        print(f"  {model}: {done} failed runs end with DONE, of which "
              f"{c['step_cap_admitted']} admit the step cap -> false claims of success = "
              f"{c['claims_done']}")

    if a.images:
        g = [r for r in rows if r.get("cmp")]
        print(f"\nNo-op repetition, {len(g)} runs with a readable trajectory:")
        for model in ("astra", "sonnet"):
            for v in ("SUCCESS", "FAILURE"):
                s = [r for r in g if r["model"] == model and r["verdict"] == v]
                if not s:
                    continue
                cmp_ = sum(r["cmp"] for r in s); un = sum(r["unchanged"] for r in s)
                print(f"  {model:7} {v:8} runs={len(s):>4} comparisons={cmp_:>6} "
                      f"screen-unchanged={un:>5} ({100*un/max(1,cmp_):>4.1f}%) "
                      f"identical-repeats={sum(r['repeats'] for r in s):>4} "
                      f"REPEAT+UNCHANGED={sum(r['repeat_noop'] for r in s):>4}")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "f2_diagnostics.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"\nper-run table: {outdir / 'f2_diagnostics.csv'} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
