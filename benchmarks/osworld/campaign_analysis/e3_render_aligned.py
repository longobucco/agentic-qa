"""V2 follow-up: an artefact rendering aligned with what the official checker actually compares.

    .venv/bin/python benchmarks/osworld/campaign_analysis/e3_render_aligned.py [--show N]

V2 found E3 (b) wrong on 16 of 16 adjudicable disagreements where it *had* the saved artefacts.
The cause is not a missing channel, it is that `e3_verifier.render_file` shows the judge something
other than the predicate the checker evaluates:

  `compare_table` / `sheet_data` loads both sheets with pandas, rounds to `precision` and calls
  `DataFrame.equals` -- so it compares **values and shape**, and an agent-added helper column fails
  the task with every required value correct. The current rendering lists cells and formulas and
  never states the shape.

  `compare_pptx_files` compares slide count, shapes, text and **fills with strict RGB**
  (`color_tolerance=0`). The current rendering gives shape type, position, size and text, and never
  the fill.

This module adds the missing view. `e3_verifier.render_file` is deliberately left untouched: X13's
results were produced with it and changing it would invalidate them.

Two parts, because the gap is not only visual:
  (a) `render_file_aligned` -- the rendering, stating the pandas shape and value grid for
      spreadsheets and the fills and colours for slides;
  (b) `IMPLICIT_RULES` -- the checker semantics a task leaves unsaid, which an auditor needs in
      order to read the rendering correctly ("the compared sheet must hold exactly the required
      cells, extras included"). This is the "library of domain rules that tasks leave implicit" of
      Veritas (arXiv 2611.13336, logged 12/11), applied to the auditor and never to the agent.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E  # noqa: E402

# Captured at import: judge_aligned patches E.render_file with render_file_aligned, so calling
# E.render_file from inside it would recurse into itself.
_ORIG_RENDER_FILE = E.render_file

TABLES = HERE / "tables"

# What each checker family compares, stated for the auditor. Keyed by the evaluator's func name.
IMPLICIT_RULES = {
    "compare_table": (
        "`sheet_data` loads both sheets with pandas, rounds to the rule's `precision` (default 4) "
        "and calls DataFrame.equals. It therefore compares VALUES AND SHAPE: a helper column, an "
        "extra header row or a shifted table fails the task even when every value the instruction "
        "asks for is correct. Formulas are irrelevant -- pandas reads the computed value. A "
        "`chart` rule compares only the chart properties it lists."),
    "compare_pptx_files": (
        "python-pptx comparison of slide count, shapes, text and FILLS, with colour tolerance 0 by "
        "default: an exact RGB match is required. Text being right says nothing about a background "
        "or shape fill."),
    "check_include_exclude": (
        "the checker runs a shell command and matches substrings. It verifies the command's output, "
        "not that an application is usable: a binary present on PATH satisfies `which <app>`."),
}


def _xlsx_pandas_view(path, max_rows=40, max_cols=20):
    """The sheet exactly as `sheet_data` loads it: shape first, then the value grid."""
    import pandas as pd
    out = []
    book = pd.read_excel(path, sheet_name=None, header=None)
    for name, df in book.items():
        out.append(f"## sheet {name!r} AS THE CHECKER LOADS IT (pandas, header=None): "
                   f"shape = {df.shape[0]} rows x {df.shape[1]} columns")
        out.append("   (sheet_data compares this grid with DataFrame.equals: shape must match too)")
        view = df.iloc[:max_rows, :max_cols]
        for idx, row in view.iterrows():
            cells = [("" if pd.isna(v) else str(v))[:24] for v in row.tolist()]
            out.append(f"   r{idx}: " + " | ".join(cells))
        if df.shape[0] > max_rows or df.shape[1] > max_cols:
            out.append(f"   ... truncated to {max_rows}x{max_cols}")
    return "\n".join(out)


def _rgb(obj):
    try:
        if obj is None or obj.type is None:
            return None
        return str(obj.rgb) if getattr(obj, "rgb", None) is not None else f"theme:{obj.theme_color}"
    except Exception:
        return None


def _pptx_fill_view(path):
    """Fills and font colours, which compare_pptx_files checks with strict RGB."""
    import pptx
    pr = pptx.Presentation(path)
    out = ["## slides AS THE CHECKER COMPARES THEM (fills and colours, exact RGB)"]
    for i, s in enumerate(pr.slides, 1):
        bg = None
        try:
            bg = _rgb(s.background.fill.fore_color) if s.background.fill.type is not None else None
        except Exception:
            bg = None
        out.append(f"   slide {i}: background fill = {bg}")
        for sh in s.shapes:
            fill = None
            try:
                fill = _rgb(sh.fill.fore_color) if sh.fill.type is not None else None
            except Exception:
                pass
            fonts = []
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    for r in para.runs:
                        c = _rgb(r.font.color)
                        if c:
                            fonts.append(f"{r.text[:16]!r}:{c}")
            out.append(f"     - {sh.name!r} fill={fill}"
                       + (f" fontcolours={fonts}" if fonts else ""))
    return "\n".join(out)


def render_file_aligned(p):
    """The current rendering, plus the view the checker's predicate actually acts on."""
    p = Path(p)
    base = _ORIG_RENDER_FILE(p)
    extra = ""
    try:
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            extra = _xlsx_pandas_view(p)
        elif p.suffix.lower() == ".pptx":
            extra = _pptx_fill_view(p)
    except Exception as e:
        extra = f"[aligned view unavailable: {type(e).__name__}: {e}]"
    return base + ("\n\n" + extra if extra else "")


def adjudicated_with_artefacts():
    """The V2 cases this is meant to fix: adjudicable disagreements where E3 had the artefacts."""
    adj = json.loads((TABLES / "f1_adjudication.json").read_text())["runs"]
    out = []
    for r in adj:
        if r["outcome"] not in ("checker_correct", "checker_correct_e3_error",
                                "checker_weakness_confirmed", "checker_false_negative_confirmed"):
            continue
        d = Path(r["run"])
        try:
            ev = json.loads((d / "eval.json").read_text())
        except (OSError, ValueError):
            continue
        arts = [d / "eval_artifacts" / a["name"] for a in ev.get("eval_artifacts", [])
                if a.get("kept") and not a.get("is_gold")
                and (d / "eval_artifacts" / a["name"]).is_file()]
        if arts:
            out.append((r, arts))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="print the aligned view of the first N runs")
    a = ap.parse_args(argv)
    cases = adjudicated_with_artefacts()
    print(f"V2 follow-up — {len(cases)} adjudicable disagreements where E3 had saved artefacts\n")
    print(f"{'task':10} {'model':7} {'official':9} {'E3':9} {'artefacts':>9} {'shape stated?':>14}")
    print("-" * 64)
    gained = 0
    for r, arts in cases:
        states = False
        for p in arts:
            if p.suffix.lower() in (".xlsx", ".xlsm", ".pptx"):
                aligned = render_file_aligned(p)
                if "AS THE CHECKER" in aligned:
                    states = True
        gained += states
        print(f"{r['task8']:10} {r['model']:7} {r['official']:9} {r['e3']:9} {len(arts):>9} "
              f"{('yes' if states else 'no (not a sheet/slide artefact)'):>14}")
    print(f"\nthe aligned view adds the checker's own predicate for {gained} of {len(cases)} cases")
    for r, arts in cases[:a.show]:
        print(f"\n{'='*70}\n{r['task8']} {r['model']} — official {r['official']}, E3 {r['e3']}")
        print(f"human verdict: {r['outcome']}\nevidence: {r['evidence'][:200]}")
        for p in arts:
            if p.suffix.lower() in (".xlsx", ".xlsm", ".pptx"):
                print(f"\n--- aligned view of {p.name} ---")
                print(render_file_aligned(p).split("\n\n", 1)[-1][:1800])




# --- experimental judge variants ---------------------------------------------------------------
# d1 = frozen variant (b) with the ALIGNED RENDERING only.
# d2 = d1 plus the IMPLICIT_RULES header for the task's checker.
# Both reuse variant (b)'s frozen criteria, so the criteria are held constant and the ablation
# isolates the rendering from the rules. e3_verifier itself is never modified: the two hooks it
# exposes (render_file, FIXED_INSPECTION) are patched for the duration of the call.
RULES_HEADER = """BEFORE YOU READ THE FILES — how this task's official checker actually decides.
These are the checker's semantics, which the task statement leaves implicit. Judge against them.

{rules}

"""


def _checker_funcs(task):
    f = (task.get("evaluator") or {}).get("func")
    return [f] if isinstance(f, str) else list(f or [])


def judge_aligned(variant, row, task, with_rules):
    """Run e3_verifier's judge with the aligned rendering (and optionally the implicit rules)."""
    import e3_verifier as EV
    out = EV.RESULTS / "_e3" / f"verdicts_{variant}" / (
        f"{row['model']}_{row['task']}_{row['verdict']}.json")
    if out.exists():
        return "cached"
    rules = [IMPLICIT_RULES[f] for f in _checker_funcs(task) if f in IMPLICIT_RULES]
    orig_render, orig_insp = EV.render_file, EV.FIXED_INSPECTION
    try:
        EV.render_file = render_file_aligned
        if with_rules and rules:
            EV.FIXED_INSPECTION = (RULES_HEADER.format(rules="\n".join(f"- {r}" for r in rules))
                                   + orig_insp)
        EV.judge(variant, row, task, out=out)
    finally:
        EV.render_file, EV.FIXED_INSPECTION = orig_render, orig_insp
    return "done" if out.exists() else "failed"


def cmd_run(argv=None):
    import e3_verifier as EV
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=("d1", "d2"), required=True)
    a = ap.parse_args(argv)
    T = EV.tasks()
    cases = adjudicated_with_artefacts()
    print(f"variant {a.variant}: {len(cases)} runs")
    for r, _ in cases:
        task = T[r["task8"]] if r["task8"] in T else next(
            (v for k, v in T.items() if k.startswith(r["task8"])), None)
        if task is None:
            print(f"  {r['task8']}: task not found"); continue
        row = {"model": r["model"], "task": task["id"], "verdict": r["official"],
               "run": str(Path(r["run"]).relative_to(EV.RESULTS))
               if str(r["run"]).startswith(str(EV.RESULTS)) else r["run"]}
        EV.wait_for_quota()
        print(f"  {r['task8']} {r['model']:7} {r['official']:8} -> "
              f"{judge_aligned(a.variant, row, task, with_rules=(a.variant == 'd2'))}", flush=True)




def cmd_compare(argv=None):
    """Did the aligned rendering change E3's verdicts, and did it change them the RIGHT way?

    The human labels say which instrument was right on each disagreement, so a flip is only an
    improvement when it moves E3 onto the human's side.
    """
    import e3_verifier as EV
    argparse.ArgumentParser().parse_args(argv)
    adj = {(r["task8"], r["model"], r["official"]): r for r in
           json.loads((TABLES / "f1_adjudication.json").read_text())["runs"]}
    E3_RIGHT = {"checker_weakness_confirmed", "checker_false_negative_confirmed"}

    def load(variant, r, task_id):
        p = (EV.RESULTS / "_e3" / f"verdicts_{variant}"
             / f"{r['model']}_{task_id}_{r['official']}.json")
        try:
            return json.loads(p.read_text())["e3"]
        except (OSError, ValueError, KeyError):
            return None

    T = EV.tasks()
    rows = []
    for r, _ in adjudicated_with_artefacts():
        tid = next((k for k in T if k.startswith(r["task8"])), None)
        if tid is None:
            continue
        human_says_e3_right = r["outcome"] in E3_RIGHT
        rows.append({"task8": r["task8"], "model": r["model"], "official": r["official"],
                     "human": "E3 right" if human_says_e3_right else "checker right",
                     "b": load("b", r, tid), "d1": load("d1", r, tid), "d2": load("d2", r, tid),
                     "e3_right": human_says_e3_right})

    print(f"{'task':10} {'model':7} {'official':9} {'human':14} {'b':>12} {'d1':>12} {'d2':>12}")
    print("-" * 80)
    for x in rows:
        print(f"{x['task8']:10} {x['model']:7} {x['official']:9} {x['human']:14} "
              f"{str(x['b']):>12} {str(x['d1']):>12} {str(x['d2']):>12}")

    def agrees(v, x):
        """E3's verdict matches the human when it says 'done' exactly where the human says E3 was
        right about the checker being wrong."""
        if v is None:
            return None
        said_checker_wrong = (v == "done") if x["official"] == "FAILURE" else (v == "not_done")
        return said_checker_wrong == x["e3_right"]

    print()
    for variant in ("b", "d1", "d2"):
        got = [agrees(x[variant], x) for x in rows]
        done = [g for g in got if g is not None]
        if not done:
            print(f"  {variant}: no verdicts yet")
            continue
        print(f"  {variant}: correct on {sum(done)}/{len(done)} of the adjudicable disagreements "
              f"({100*sum(done)/len(done):.0f}%)")
    flips = [x for x in rows if x["d2"] is not None and x["d2"] != x["b"]]
    print(f"\n  verdicts changed by the aligned view+rules (d2 vs b): {len(flips)} of {len(rows)}")


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) > 1 and _s.argv[1] == "run":
        cmd_run(_s.argv[2:])
    elif len(_s.argv) > 1 and _s.argv[1] == "compare":
        cmd_compare(_s.argv[2:])
    else:
        main()
