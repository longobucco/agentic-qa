"""G10 -- click-targeting quality, and the grounding arm's pre-registered gate.

TWO JOBS. Run against one results tree it reports the targeting signal that motivated the
grounding harness. Run with `--compare` it evaluates the arm against its gate, on the task set
present in BOTH trees.

THE SIGNAL IS MODEL-DEPENDENT, AND ON PINNED SONNET 5 IT IS ABSENT. This matters more than
anything else in this docstring, because the grounding harness was built on the opposite belief.

The targeting gap was first measured on `agent_computer`, which config.py labels as the unpinned
historical tree and which really is mixed: of its 982 runs, 326 were served by claude-sonnet-5,
296 by claude-sonnet-4-6 and 5 by claude-opus-4-8, with model_requested=None on all 982. There the
gap is real and significant:

  agent_computer (MIXED MODEL)   clicks/run   re-click %   screenshots/click
  always-pass                         6.4         8.7%          1.28
  always-fail                         7.6        11.8%          1.71
  flaky                              12.5        15.7%          1.74
  -> z = 2.74 on the re-click proportion, p < 0.05, flaky the extreme on all three

Re-run on `agent_computer_sonnet5` (882 runs, 848 served by claude-sonnet-5) it disappears, and
the sign flips:

  agent_computer_sonnet5         clicks/run   re-click %   screenshots/click
  always-pass                         5.3         4.3%          0.98
  always-fail                         4.6         3.4%          1.17
  flaky                               7.3         4.3%          1.08
  -> z = -1.37, NOT significant, always-fail BETTER than always-pass

Sonnet 5 re-clicks at roughly half the mixed tree's rate and uses fewer clicks per run, and its
always-fail tasks are not the ones it targets badly. Targeting churn reads as a capability
signature of the weaker models in the mixed tree, not as the mechanism behind Sonnet 5's residual
failures. So this module's own gate, run before any rollout spend, says the arm's premise does not
hold for the model the project is about -- see docs/grounding-harness-plan.md Section 5.

WHAT STILL HOLDS ON THE PINNED TREE (833 agent-scored runs, 851 transcripts, 56.8% pass rate;
tasks: always-pass 137 / flaky 36 / always-fail 104, so a verification-only ceiling of 62.5%):
the turn budget is not binding (median 17 turns in both buckets, 0.0% at the 150 cap), the agent
already reopens and parses its own output files (73.2% of always-fail runs that use run_python),
and a11y_tree is still barely used (137 calls, 0.8%, against screenshot's 4682 at 27.3%). The
underuse is genuine; what the data does not support is that closing it would move outcomes.

WHAT A RE-CLICK IS, AND WHAT IT IS NOT. A click landing within 8px of the immediately preceding
click with no intervening type/key: the first one did not do what the agent expected. It is a
proxy and it is confounded -- harder tasks get more clicks, and more clicks mean more chances to
re-click -- so the gate below does not rest on it alone. Coordinate clicks issued inside
run_python are invisible here, equally in both trees, so every rate is a lower bound on a subset
of the action stream.

  python -m benchmarks.osworld.analysis.g10_grounding_signal [--system X] [--compare Y] [--json]
"""
import json
import math
import re
import sys
from collections import Counter, defaultdict

from benchmarks.osworld import config

CLICK_TOOLS = {"click", "double_click", "right_click", "triple_click"}
TEXT_TOOLS = {"type", "key"}
NEAR_PX = 8

# click_element takes a description, not coordinates -- the point it actually clicked is in its
# RESULT text ("clicked push-button 'Bold' at (212,62) [rank 1/3 ...]"), so the two trees stay
# measurable on the same footing. Kept in sync with mcp/grounding_tools.click_element's own
# wording by test_g10_grounding_signal.
_CLICKED_AT_RE = re.compile(r"\bat \((\d+),(\d+)\)")
_NO_EFFECT = "the accessibility tree is unchanged"
_UNRESOLVED = "no element resolved"

# Gate, pre-registered before the arm runs (docs/grounding-harness-plan.md). Two conditions,
# because the mechanism could plausibly improve targeting without improving outcomes -- and that
# result, while real, would not justify the arm.
GATE_RECLICK_DROP = 0.30      # relative reduction in the re-click rate
GATE_TASKS_IMPROVED = 3       # tasks gaining at least one SUCCESS


def _verdicts(results_dir, system):
    """{task_id: {run_dir_name: verdict}} for the agent-scored runs of one tree."""
    base = results_dir / system
    out = defaultdict(dict)
    if not base.is_dir():
        return out
    for p in base.glob("*/run_[0-9]/eval.json"):
        try:
            ev = json.loads(p.read_text())
        except Exception:
            continue
        if ev.get("verdict") in ("SUCCESS", "FAILURE"):
            out[p.parent.parent.name][p.parent.name] = ev["verdict"]
    return out


def _buckets(verdicts):
    out = {"always-pass": [], "flaky": [], "always-fail": []}
    for tid, runs in verdicts.items():
        vs = list(runs.values())
        if len(vs) < 2:
            continue
        key = ("always-pass" if all(v == "SUCCESS" for v in vs)
               else "always-fail" if all(v == "FAILURE" for v in vs) else "flaky")
        out[key].append(tid)
    return out


def _tool_stream(path):
    """[(tool_name, input_dict, result_text)] in order. The result text is needed because
    click_element reports its resolved coordinates and its no-effect warning there, not in the
    call."""
    calls, results = [], {}
    for line in path.read_text(errors="replace").splitlines():
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                calls.append((b.get("id"), b.get("name", "").split("__")[-1], b.get("input") or {}))
            elif b.get("type") == "tool_result":
                c = b.get("content")
                results[b.get("tool_use_id")] = c if isinstance(c, str) else json.dumps(c)
    return [(name, inp, results.get(cid, "")) for cid, name, inp in calls]


def _xy(inp, result):
    try:
        return float(inp["x"]), float(inp["y"])
    except (KeyError, TypeError, ValueError):
        pass
    m = _CLICKED_AT_RE.search(result or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def targeting(results_dir, system, *, task_ids=None):
    """Targeting metrics for one tree, optionally restricted to `task_ids`."""
    verdicts = _verdicts(results_dir, system)
    base = results_dir / system
    buckets = _buckets(verdicts)
    per_bucket = {}
    grounding = Counter()

    for label, ids in buckets.items():
        ids = [t for t in ids if task_ids is None or t in task_ids]
        runs = clicks = reclicks = shots = 0
        for tid in ids:
            for run_name in verdicts[tid]:
                fp = base / tid / run_name / "conversation.jsonl"
                if not fp.exists():
                    continue
                stream = _tool_stream(fp)
                if not stream:
                    continue
                runs += 1
                last = None
                for name, inp, result in stream:
                    if name == "screenshot":
                        shots += 1
                    if name in TEXT_TOOLS:
                        last = None          # typing proves the click landed
                    if name == "click_element":
                        grounding["click_element"] += 1
                        if _UNRESOLVED in result:
                            grounding["unresolved"] += 1
                        elif _NO_EFFECT in result:
                            grounding["no_effect"] += 1
                    elif name in ("find_element", "list_elements"):
                        grounding[name] += 1
                    if name in CLICK_TOOLS or name == "click_element":
                        xy = _xy(inp, result)
                        if not xy:
                            continue          # unresolved click_element: nothing was clicked
                        clicks += 1
                        if last and math.dist(xy, last) <= NEAR_PX:
                            reclicks += 1
                        last = xy
        per_bucket[label] = {
            "tasks": len(ids), "runs": runs, "clicks": clicks, "reclicks": reclicks,
            "screenshots": shots,
            "reclick_rate": reclicks / clicks if clicks else None,
            "clicks_per_run": clicks / runs if runs else None,
            "shots_per_click": shots / clicks if clicks else None,
        }
    totals = {
        "clicks": sum(b["clicks"] for b in per_bucket.values()),
        "reclicks": sum(b["reclicks"] for b in per_bucket.values()),
        "runs": sum(b["runs"] for b in per_bucket.values()),
    }
    totals["reclick_rate"] = (totals["reclicks"] / totals["clicks"]) if totals["clicks"] else None
    return {"system": system, "buckets": per_bucket, "totals": totals,
            "grounding_tools": dict(grounding),
            "successes_by_task": {t: sum(1 for v in r.values() if v == "SUCCESS")
                                  for t, r in verdicts.items()}}


def two_proportion_z(k1, n1, k2, n2):
    """z for two independent proportions; None when either sample is empty."""
    if not n1 or not n2:
        return None
    p1, p2 = k1 / n1, k2 / n2
    pool = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se else 0.0


def _print_one(rep):
    print(f"\n=== {rep['system']} ===")
    print(f"  {'bucket':12} {'tasks':>6} {'runs':>5} {'clicks':>7} {'clicks/run':>11} "
          f"{'re-click %':>11} {'shots/click':>12}")
    for label in ("always-pass", "flaky", "always-fail"):
        b = rep["buckets"].get(label)
        if not b or not b["runs"]:
            continue
        print(f"  {label:12} {b['tasks']:6} {b['runs']:5} {b['clicks']:7} "
              f"{b['clicks_per_run']:11.1f} {100*b['reclick_rate']:10.1f}% "
              f"{b['shots_per_click']:12.2f}")

    ap, af = rep["buckets"].get("always-pass"), rep["buckets"].get("always-fail")
    if ap and af and ap["clicks"] and af["clicks"]:
        z = two_proportion_z(af["reclicks"], af["clicks"], ap["reclicks"], ap["clicks"])
        print(f"  re-click always-fail vs always-pass: {100*af['reclick_rate']:.1f}% vs "
              f"{100*ap['reclick_rate']:.1f}%  (z = {z:.2f}, "
              f"{'significant' if abs(z) > 1.96 else 'NOT significant'} at p<0.05)")

    g = rep["grounding_tools"]
    if g:
        ce = g.get("click_element", 0)
        print(f"  grounding tools: click_element {ce}, find_element {g.get('find_element', 0)}, "
              f"list_elements {g.get('list_elements', 0)}")
        if ce:
            # Measured by the harness itself rather than inferred from an 8px proxy: this is the
            # cleanest available read on how often a click did nothing.
            print(f"    of those click_element calls: {g.get('unresolved', 0)} resolved nothing "
                  f"({100*g.get('unresolved', 0)/ce:.1f}%), {g.get('no_effect', 0)} reported "
                  f"no effect ({100*g.get('no_effect', 0)/ce:.1f}%)")


def _print_gate(base_rep, arm_rep, shared):
    print(f"\n=== GATE (pre-registered, docs/grounding-harness-plan.md) ===")
    print(f"  compared on the {len(shared)} task(s) present in both trees")

    b_tot, a_tot = base_rep["totals"], arm_rep["totals"]
    if not b_tot["clicks"] or not a_tot["clicks"]:
        print("  INCONCLUSIVE: one of the trees has no measurable clicks")
        return False
    drop = (b_tot["reclick_rate"] - a_tot["reclick_rate"]) / b_tot["reclick_rate"]
    z = two_proportion_z(a_tot["reclicks"], a_tot["clicks"], b_tot["reclicks"], b_tot["clicks"])
    c1 = drop >= GATE_RECLICK_DROP
    # Phrased as a reduction, not a signed delta: a positive `drop` is an improvement, so printing
    # it with a +/- sign next to "need -30%" reads as the opposite of what it means.
    direction = "reduction" if drop >= 0 else "INCREASE"
    print(f"  [{'PASS' if c1 else 'FAIL'}] re-click rate {100*b_tot['reclick_rate']:.1f}% -> "
          f"{100*a_tot['reclick_rate']:.1f}% = {100*abs(drop):.1f}% relative {direction} "
          f"(need a {100*GATE_RECLICK_DROP:.0f}% reduction, z = {z:.2f})")

    base_succ, arm_succ = base_rep["successes_by_task"], arm_rep["successes_by_task"]
    improved = [t for t in shared if arm_succ.get(t, 0) > base_succ.get(t, 0)]
    worsened = [t for t in shared if arm_succ.get(t, 0) < base_succ.get(t, 0)]
    c2 = len(improved) >= GATE_TASKS_IMPROVED
    print(f"  [{'PASS' if c2 else 'FAIL'}] tasks gaining a SUCCESS: {len(improved)} "
          f"(need >={GATE_TASKS_IMPROVED}); tasks losing one: {len(worsened)}")
    if improved:
        print(f"       improved: {', '.join(t[:8] for t in sorted(improved))}")
    if worsened:
        print(f"       worsened: {', '.join(t[:8] for t in sorted(worsened))}")

    verdict = c1 and c2
    print(f"\n  GATE {'PASSED -- scale up' if verdict else 'NOT PASSED -- do not scale'}")
    if not verdict:
        print("  (a pass on targeting alone is a real finding about the mechanism, and still not "
              "a reason to scale: the arm exists to move outcomes.)")
    return verdict


def _main():
    argv = sys.argv[1:]
    system = config.resolve_system(config.system_from_argv())
    compare = None
    for i, a in enumerate(argv):
        if a == "--compare" and i + 1 < len(argv):
            compare = argv[i + 1]
        elif a.startswith("--compare="):
            compare = a.split("=", 1)[1]

    if not compare:
        rep = targeting(config.RESULTS_DIR, system)
        if "--json" in argv:
            print(json.dumps(rep, indent=2))
        else:
            _print_one(rep)
        return

    shared = set(_verdicts(config.RESULTS_DIR, system)) & set(
        _verdicts(config.RESULTS_DIR, compare))
    base_rep = targeting(config.RESULTS_DIR, system, task_ids=shared)
    arm_rep = targeting(config.RESULTS_DIR, compare, task_ids=shared)
    if "--json" in argv:
        print(json.dumps({"baseline": base_rep, "arm": arm_rep,
                          "shared_tasks": sorted(shared)}, indent=2))
        return
    _print_one(base_rep)
    _print_one(arm_rep)
    _print_gate(base_rep, arm_rep, shared)


if __name__ == "__main__":
    _main()
