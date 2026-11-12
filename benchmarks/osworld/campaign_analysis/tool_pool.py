"""How much failure mass could an app-level tool actually reach? Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/tool_pool.py [--out tables]

The shell hypothesis is falsified: X12 gave the agent `run_command`, adoption was 85% of class-F
runs and 86% of its steps, and the pass rate moved −1.8 points. The audit showed *what* the shell
was used for — overwhelmingly read-only inspection of the saved artefact (openpyxl, unzip+grep of
the Office XML) — so the agent already reads its work back and still fails.

That leaves a different hypothesis untested, and this file sizes it before anyone builds anything.
V2 found the offline verifier wrong because its view of an artefact was not aligned with the
predicate the checker evaluates: it read formulas where `sheet_data` compares values and shape, and
text where `compare_pptx_files` compares fills. **The agent may be failing for the same reason.** A
tool that returns the artefact as the checker loads it is generic — it tells the agent nothing
about the task, the checker's expected values or the gold — and is exactly what neither the GUI nor
a shell gives today.

The measurement: for every FAILURE that kept both a result and a gold artefact, classify what the
difference IS, mechanically, from the two renderings. The classes are chosen so that each one says
whether a predicate-aligned read-back could have let the agent notice the problem *before*
answering:

  shape         -- the result has different dimensions than the gold (extra column, extra header
                   row, shifted table). `DataFrame.equals` rejects on this alone. A read-back that
                   states the shape makes it visible; a cell-by-cell view does not.
  content       -- same shape, different values. Visible to any read-back, including today's shell.
  formatting    -- the renderings differ only in fills, fonts or number formats.
  empty_result  -- the result artefact is empty or unreadable: nothing to read back.
  structural    -- different sheets/slides/sections altogether: the agent did something else.
"""
import argparse
import csv
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E  # noqa: E402

RESULTS = HERE.parent / "results"
SHAPE_RE = re.compile(r"dims ([A-Z]+\d+:[A-Z]+\d+)")
SHEET_RE = re.compile(r"## sheet '([^']*)'")
SLIDE_RE = re.compile(r"## slide (\d+)")
FMT_RE = re.compile(r"\[(fmt=|fill=|bold|italic)")


def classify(res_text, gold_text):
    if not res_text.strip() or res_text.startswith("[could not render"):
        return "empty_result"
    r_shapes, g_shapes = SHAPE_RE.findall(res_text), SHAPE_RE.findall(gold_text)
    if r_shapes != g_shapes:
        return "shape"
    if SHEET_RE.findall(res_text) != SHEET_RE.findall(gold_text):
        return "structural"
    if SLIDE_RE.findall(res_text) != SLIDE_RE.findall(gold_text):
        return "structural"
    diff = [x for x in difflib.unified_diff(res_text.splitlines(), gold_text.splitlines(),
                                            n=0, lineterm="") if x[:1] in "+-" and x[1:2] != "+"
            and not x.startswith("---")]
    if not diff:
        return "identical_rendering"
    if all(FMT_RE.search(x) for x in diff):
        return "formatting"
    return "content"


def rows():
    out = []
    for p in RESULTS.glob("agent_computer_*_official/*/run_*/eval.json"):
        tree = p.parents[2].name
        if "pkgfix3" not in tree:
            continue
        try:
            ev = json.loads(p.read_text())
        except ValueError:
            continue
        if ev.get("verdict") != "FAILURE":
            continue
        arts = [a for a in ev.get("eval_artifacts", []) if a.get("kept")]
        res = [a["name"] for a in arts if not a.get("is_gold")]
        gold = [a["name"] for a in arts if a.get("is_gold")]
        if not res or not gold:
            continue
        d = p.parent / "eval_artifacts"
        if "nullagent" in tree:
            continue        # an agent that does nothing says nothing about a tool's reach
        model = "astra" if "gpt6astra" in tree else "sonnet"
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("_official", "").strip("_") or "a0")
        kinds = []
        for r, g in zip(res, gold):
            if not (d / r).is_file() or not (d / g).is_file():
                continue
            kinds.append(classify(E.render_file(d / r), E.render_file(d / g)))
        if not kinds:
            continue
        # one class per run: the most "structural" problem present
        order = ["empty_result", "structural", "shape", "content", "formatting",
                 "identical_rendering"]
        kind = min(kinds, key=lambda k: order.index(k) if k in order else 99)
        out.append({"model": model, "arm": arm, "task": p.parents[1].name[:8],
                    "run": p.parent.name, "ext": Path(res[0]).suffix.lower(), "kind": kind})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rs = rows()
    print(f"tool_pool — {len(rs)} FAILURE runs on pkgfix3 that kept both a result and a gold "
          f"artefact\n")

    by = Counter(r["kind"] for r in rs)
    print(f"  {'class':20} {'runs':>5} {'share':>7}  reachable by a predicate-aligned read-back?")
    reach = {"shape": "YES — and only by one that states the shape",
             "formatting": "YES — only by one that exposes fills and formats",
             "content": "partly — today's shell already shows values",
             "structural": "partly — the agent did something else",
             "empty_result": "no — nothing to read back",
             "identical_rendering": "no — the difference is outside the rendering"}
    for k, v in by.most_common():
        print(f"  {k:20} {v:>5} {100*v/len(rs):>6.1f}%  {reach.get(k,'')}")

    print("\nBy model and arm:")
    agg = defaultdict(Counter)
    for r in rs:
        agg[(r["model"], r["arm"])][r["kind"]] += 1
    cols = [k for k, _ in by.most_common()]
    print(f"  {'model':7} {'arm':9} {'runs':>5} " + " ".join(c[:9].rjust(10) for c in cols))
    for key in sorted(agg):
        c = agg[key]
        print(f"  {key[0]:7} {key[1]:9} {sum(c.values()):>5} "
              + " ".join(str(c[k]).rjust(10) for k in cols))

    aligned = by["shape"] + by["formatting"]
    print(f"\n  Pool a predicate-aligned read-back could reach and a shell could not: "
          f"**{aligned} of {len(rs)} runs ({100*aligned/max(1,len(rs)):.0f}%)**")
    print("  (ceiling, not an estimate: seeing the problem is necessary, not sufficient)")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "tool_pool.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    print(f"\nper-run table: {outdir / 'tool_pool.csv'}")


if __name__ == "__main__":
    main()
