"""X18: cost and efficiency of the arms, from the archived result.json. Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/x18_cost.py [--out tables]

Pre-registered in EXPERIMENTS.md X18 (5/11), with **amendment 1 of 12/11** (user decision, option
(b)): cost is reported as raw tokens **plus** a cache-adjusted column, and the cache-adjusted figure
is the headline. Raw tokens systematically favour the arm that re-reads the same screenshots,
because those are cache hits, and penalise the arm that issues short commands (arXiv 2607.10569,
which varies the tool surface on Claude Code and Codex CLI with model, harness and prompt fixed and
finds pass rates equal in every cell while only cache-adjusted cost moves). Both columns are kept so
the pre-registered quantity stays visible.

A run counts iff eval.json carries a verdict (core.results.is_done). infra_error.json is an
append-only log of attempts that died before a verdict, so a run holding both is a retry that
succeeded and counts -- getting this wrong dropped 339 judged runs on the first pass (12/11).
"""
import argparse
import csv
import json
import os
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
SETS = HERE.parent / "data" / "phase3_sets.json"

# What astra_common.estimated_api_cost prices, stated per run in agent_cost_basis.
PRICE_UNCACHED_IN, PRICE_CACHED_IN, PRICE_OUT = 10.0, 1.0, 50.0   # USD per 1M tokens


def wall_seconds(prov):
    a, b = prov.get("started_at"), prov.get("finished_at")
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except ValueError:
        return None


def cache_adjusted_usd(r):
    """Cost with cached input priced at $1/M against $10/M uncached: the headline figure.

    Recomputed here rather than read from agent_estimated_api_cost_usd so the formula is visible
    next to the raw tokens, and so a run whose runner never wrote that field still gets a number.
    """
    i, o = r.get("agent_input_tokens"), r.get("agent_output_tokens")
    if i is None or o is None:
        return None
    cached = r.get("agent_cached_input_tokens") or 0
    uncached = max(0, i - cached)
    return (uncached * PRICE_UNCACHED_IN + cached * PRICE_CACHED_IN + o * PRICE_OUT) / 1e6


def raw_token_usd(r):
    """The same price list with the cache ignored: every input token at $10/M. This is the figure
    amendment 1 says not to lead with -- it is kept to show how much of a cost gap is just cache."""
    i, o = r.get("agent_input_tokens"), r.get("agent_output_tokens")
    if i is None or o is None:
        return None
    return (i * PRICE_UNCACHED_IN + o * PRICE_OUT) / 1e6


def load():
    sets = json.loads(SETS.read_text())["sets"]
    setof = {t: n for n, s in sets.items() if n != "population361" for t in s["tasks"]}
    rows = []
    for p in RESULTS.glob("agent_computer_*_official/*/run_*/result.json"):
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
        if "pkgfix3" not in tree:
            continue   # X18 is the Phase 3 arms; the Phase 2 campaigns live on the archive
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else "?")
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("_official", "").strip("_") or "a0")
        task = p.parents[1].name
        prov = r.get("provenance") or {}
        cmds = p.parent / "commands.jsonl"
        rows.append({
            "model": model, "arm": arm, "task": task, "set": setof.get(task, "?"),
            "verdict": verdict, "steps": r.get("agent_steps_used"),
            "in": r.get("agent_input_tokens"), "out": r.get("agent_output_tokens"),
            "cached": r.get("agent_cached_input_tokens"),
            "reasoning": r.get("agent_reasoning_output_tokens"),
            "wall": wall_seconds(prov),
            "usd_adj": cache_adjusted_usd(r), "usd_raw": raw_token_usd(r),
            "mcp_calls": r.get("agent_mcp_tool_calls"),
            "commands": sum(1 for _ in cmds.open()) if cmds.exists() else 0,
        })
    return rows


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def fmt(v, spec=".2f"):
    return "-" if v is None else format(v, spec)


def per_arm(rows):
    out = {}
    for key in sorted({(r["model"], r["arm"]) for r in rows}):
        g = [r for r in rows if (r["model"], r["arm"]) == key]
        succ = [r for r in g if r["verdict"] == "SUCCESS"]
        adj = [r["usd_adj"] for r in g if r["usd_adj"] is not None]
        raw = [r["usd_raw"] for r in g if r["usd_raw"] is not None]
        cached_share = [(r["cached"] or 0) / r["in"] for r in g if r["in"]]
        out[key] = {
            "runs": len(g), "pass": len(succ) / len(g) if g else 0,
            "steps_med": med([r["steps"] for r in g]),
            "calls_med": med([r["mcp_calls"] for r in g]),
            "wall_med": med([r["wall"] for r in g]),
            "in_med": med([r["in"] for r in g]), "out_med": med([r["out"] for r in g]),
            "cached_share": med(cached_share),
            "usd_adj_med": med(adj), "usd_raw_med": med(raw),
            "usd_adj_total": sum(adj) if adj else None,
            "usd_adj_per_success": (sum(adj) / len(succ)) if adj and succ else None,
            "usd_raw_per_success": (sum(raw) / len(succ)) if raw and succ else None,
            "runs_with_commands": sum(1 for r in g if r["commands"] > 0),
        }
    return out


def paired_vs_a0(rows, model):
    """Per-task cost ratio against A0 on the same sets, median over tasks (the pre-registered
    comparison). Reported for both cost columns, so the cache's share of any gap is visible."""
    by = defaultdict(dict)
    for r in rows:
        if r["model"] != model or r["usd_adj"] is None:
            continue
        by[(r["task"], r["arm"])].setdefault("adj", []).append(r["usd_adj"])
        by[(r["task"], r["arm"])].setdefault("raw", []).append(r["usd_raw"])
    arms = sorted({a for (_, a) in by} - {"a0"})
    out = {}
    for arm in arms:
        ratios_adj, ratios_raw = [], []
        for (task, a) in list(by):
            if a != arm or (task, "a0") not in by:
                continue
            base_adj, base_raw = med(by[(task, "a0")]["adj"]), med(by[(task, "a0")]["raw"])
            arm_adj, arm_raw = med(by[(task, arm)]["adj"]), med(by[(task, arm)]["raw"])
            if base_adj:
                ratios_adj.append(arm_adj / base_adj)
            if base_raw:
                ratios_raw.append(arm_raw / base_raw)
        out[arm] = {"tasks": len(ratios_adj), "ratio_adj": med(ratios_adj),
                    "ratio_raw": med(ratios_raw)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rows = load()
    print(f"X18 — {len(rows)} judged runs on the pkgfix3 trees\n")

    print("Per arm. HEADLINE cost column is usd/adj (cached input at $1/M); usd/raw ignores the "
          "cache and is shown to expose how much of a gap is cache alone.")
    hdr = (f"{'model':7} {'arm':9} {'runs':>5} {'pass':>6} {'steps':>6} {'calls':>6} "
           f"{'wall s':>7} {'in tok':>8} {'out tok':>8} {'cached':>7} "
           f"{'usd/adj':>8} {'usd/raw':>8} {'adj/succ':>9} {'raw/succ':>9} {'cmd runs':>9}")
    print(hdr)
    print("-" * len(hdr))
    stats = per_arm(rows)
    for (model, arm), s in stats.items():
        print(f"{model:7} {arm:9} {s['runs']:>5} {100*s['pass']:>5.1f}% "
              f"{fmt(s['steps_med'], '.0f'):>6} {fmt(s['calls_med'], '.0f'):>6} "
              f"{fmt(s['wall_med'], '.0f'):>7} {fmt(s['in_med'], '.0f'):>8} "
              f"{fmt(s['out_med'], '.0f'):>8} "
              f"{('-' if s['cached_share'] is None else format(100*s['cached_share'], '.0f')+'%'):>7} "
              f"{fmt(s['usd_adj_med'], '.3f'):>8} {fmt(s['usd_raw_med'], '.3f'):>8} "
              f"{fmt(s['usd_adj_per_success'], '.3f'):>9} "
              f"{fmt(s['usd_raw_per_success'], '.3f'):>9} {s['runs_with_commands']:>9}")

    for model in ("astra", "sonnet"):
        pv = paired_vs_a0(rows, model)
        if not pv:
            continue
        print(f"\nPaired per-task cost ratio against A0 — {model} (median over shared tasks):")
        for arm, v in sorted(pv.items()):
            print(f"  {arm:9} tasks={v['tasks']:>3}  adjusted x{fmt(v['ratio_adj'], '.2f')}  "
                  f"raw x{fmt(v['ratio_raw'], '.2f')}")

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "x18_cost.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nper-run table: {outdir / 'x18_cost.csv'} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
