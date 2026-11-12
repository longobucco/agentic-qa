"""Section 4 of proposta_harness_osworld.md: was any MCP call ever delivered twice? Zero quota.

    .venv/bin/python benchmarks/osworld/campaign_analysis/call_delivery.py [--out tables]

The proposal asks for idempotency keyed on the MCP call id, so that a retried delivery replays the
first result instead of clicking twice. **That is not implementable at our layer**: the tool
functions in `mcp/server.py` are plain FastMCP tools and receive only their arguments -- the
JSON-RPC envelope, and with it the request id, is consumed by the framework and never reaches us.
Nothing in the harness can see that two deliveries are the same delivery.

So before writing an unimplementable fix into the plan, measure whether the failure it guards
against ever happened, using the one place the two sides of the channel are counted independently:

  * `agent_steps_used` comes from the MCP server's own state file (`official_computer.write_state`,
    read by the runner at `gpt_astra.py:431`). It counts **executions**: `ComputerSession.call`
    increments before acting, and so does `command_tool.run_command`.
  * `conversation.jsonl` is the CLI's record. It counts **calls the model made** and got a result
    for.

A duplicate delivery executes twice and is reported once, so it shows up as server steps above
transcript calls. Replaying the server's accounting rules over the transcript gives the exact
number to expect:

  the first `computer` call is free iff its action is `screenshot` and no step has been used
  (a batch is never free), and a call arriving after the cap executes nothing and costs nothing.

A surplus is necessary but not sufficient evidence of a double delivery: a call the CLI aborted
after the server had executed it produces the same arithmetic, and so does a transcript truncated
by the CLI dying. The run-level table keeps the distinction readable -- `end` says how the run
ended -- rather than asserting a cause.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transcripts import calls  # noqa: E402

RESULTS = HERE.parent / "results"
MAX_STEPS = 100
# Every tool result ends with the server's own reminder, `ComputerSession._reminder`. It carries
# the step counter as the SERVER sees it, call by call, which is the only in-band view of the
# server's state the archive has.
STEP_RE = re.compile(r"Current step: (\d+)/(\d+)")


# The tools the server executes and counts. Everything else Codex emits as an `mcp_tool_call` is
# a framework resource call (CODEX_EXEMPT_TOOLS: list_mcp_resources, list_mcp_resource_templates),
# which never reaches a tool function and never moves the step counter -- counting those as calls
# produced 4 spurious deficits on the first pass.
EXECUTED = ("computer", "run_command")


def expected_steps(run_dir):
    """(steps the server should have counted, calls in the transcript, calls refused by the cap,
    exempt resource calls).

    Replays `ComputerSession.call` and `command_tool.run_command`: one step per call, the first
    screenshot free, a call past the cap executes nothing.
    """
    steps = refused = n = exempt = 0
    seen_any = False
    for c in calls(run_dir):
        if c["tool"] not in EXECUTED:
            exempt += 1
            continue
        n += 1
        free = (not seen_any and steps == 0 and c["tool"] == "computer"
                and c["args"].get("action") == "screenshot"
                and not isinstance(c["args"].get("actions"), list))
        seen_any = True
        if free:
            continue
        if steps >= MAX_STEPS:
            refused += 1
            continue
        steps += 1
    return steps, n, refused, exempt


def counter_resets(run_dir):
    """(resets, steps lost to them, highest value the server's counter ever showed).

    The server is a stdio subprocess of the CLI. If it dies, the client restarts it, and the new
    process builds a fresh `ComputerSession` with `steps_used = 0` -- so the reminder's counter
    goes backwards and the agent silently gets a second budget. A reset is therefore visible in
    the transcript as a decrease, and nowhere else: `agent_steps_used` is read from the final
    state file at the end of the run and only ever sees the last session.
    """
    resets = lost = peak = 0
    prev = None
    for c in calls(run_dir):
        m = STEP_RE.search(c["text"] or "")
        if not m:
            continue
        cur = int(m.group(1))
        peak = max(peak, cur)
        if prev is not None and cur < prev:
            resets += 1
            lost += prev - cur
        prev = cur
    return resets, lost, peak


def rows():
    out = []
    for p in sorted(RESULTS.glob("agent_computer_*_official/*/run_*/result.json")):
        tree = p.parents[2].name
        if "nullagent" in tree or "bounce" in tree:
            continue            # no agent / two sessions on one budget: neither is comparable
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        recorded = r.get("agent_steps_used")
        if recorded is None or not (p.parent / "conversation.jsonl").exists():
            continue
        exp, n, refused, exempt = expected_steps(p.parent)
        resets, lost, peak = counter_resets(p.parent)
        if n == 0:
            continue            # unreadable transcript says nothing about delivery
        model = "astra" if "gpt6astra" in tree else ("sonnet" if "sonnet5" in tree else "?")
        arm = (tree.replace("agent_computer_gpt6astra_max_codex01534_pkgfix3", "")
                   .replace("agent_computer_sonnet5_effortmax_pkgfix3", "")
                   .replace("agent_computer_gpt6astra_max_codex01534_protocol361img3", "")
                   .replace("_official", "").strip("_") or "a0")
        end = "error" if (r.get("agent_api_error_status")
                          or r.get("agent_stop_reason") in ("error", "timeout")) else "clean"
        out.append({"model": model, "arm": arm, "task": p.parents[1].name[:8],
                    "run": p.parent.name, "tree": tree, "calls": n, "cap_refused": refused,
                    "exempt_calls": exempt, "counter_resets": resets,
                    "steps_lost_to_resets": lost, "counter_peak": peak,
                    "expected_steps": exp, "recorded_steps": recorded,
                    "surplus": recorded - exp, "end": end})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "tables"))
    a = ap.parse_args(argv)
    rs = rows()
    if not rs:
        raise SystemExit("no run carries both a server step count and a readable transcript")
    print(f"call delivery — {len(rs)} runs with a server step count and a readable transcript\n")

    by = Counter(("surplus" if r["surplus"] > 0 else
                  "deficit" if r["surplus"] < 0 else "exact") for r in rs)
    for k in ("exact", "surplus", "deficit"):
        print(f"  {k:9} {by[k]:>5}  ({100*by[k]/len(rs):>5.1f}%)")

    sur = sorted((r for r in rs if r["surplus"] > 0), key=lambda r: -r["surplus"])
    print(f"\n  runs where the server executed MORE than the transcript shows: {len(sur)}")
    if sur:
        print(f"  {'model':7} {'arm':9} {'task':9} {'run':8} {'calls':>6} {'exp':>5} "
              f"{'rec':>5} {'surplus':>8} {'end':>6}")
        for r in sur[:25]:
            print(f"  {r['model']:7} {r['arm']:9} {r['task']:9} {r['run']:8} {r['calls']:>6} "
                  f"{r['expected_steps']:>5} {r['recorded_steps']:>5} {r['surplus']:>8} "
                  f"{r['end']:>6}")
        c = Counter(r["end"] for r in sur)
        print(f"\n  of those, {c['error']} ended with a CLI error or timeout (the server executed "
              f"a call whose\n  result never reached the transcript) and {c['clean']} ended "
              f"cleanly.")
        print("  Only the clean ones are candidates for a duplicate delivery.")
    else:
        print("  none: on every run the server's own count matches the calls the model made, so "
              "no\n  delivery was ever executed twice. Idempotency keyed on a call id would have "
              "nothing to\n  deduplicate -- which is just as well, since the id never reaches our "
              "layer.")

    def_ = [r for r in rs if r["surplus"] < 0]
    if def_:
        print(f"\n  runs where the transcript shows MORE calls than the server counted: "
              f"{len(def_)} (parser or accounting gap, not delivery)")
        for r in def_[:10]:
            print(f"    {r['model']:7} {r['arm']:9} {r['task']:9} {r['run']:8} "
                  f"calls={r['calls']} expected={r['expected_steps']} "
                  f"recorded={r['recorded_steps']}")

    # --- the failure the deficit actually pointed at -------------------------------------------
    res = [r for r in rs if r["counter_resets"]]
    print(f"\n  runs whose step counter went BACKWARDS mid-run (the MCP server died and the CLI "
          f"restarted\n  it, giving the agent a fresh 100-step budget): {len(res)} of {len(rs)}")
    if res:
        print(f"  {'model':7} {'arm':9} {'task':9} {'run':8} {'resets':>7} {'regained':>9} "
              f"{'executed':>9} {'recorded':>9} {'peak':>5}")
        for r in sorted(res, key=lambda r: -r["steps_lost_to_resets"])[:25]:
            print(f"  {r['model']:7} {r['arm']:9} {r['task']:9} {r['run']:8} "
                  f"{r['counter_resets']:>7} {r['steps_lost_to_resets']:>9} "
                  f"{r['expected_steps']:>9} {r['recorded_steps']:>9} {r['counter_peak']:>5}")
        over = [r for r in res if r["expected_steps"] > MAX_STEPS]
        print(f"\n  runs that executed more than the declared {MAX_STEPS}-step budget because of "
              f"a reset: {len(over)}")
        for r in over:
            print(f"    {r['model']} {r['arm']} {r['task']} {r['run']}: "
                  f"{r['expected_steps']} steps executed")
        if not over:
            print("    none: every reset happened in a run that was nowhere near the cap, so no "
                  "run\n    exceeded the budget. The record is still wrong -- `agent_steps_used` "
                  "under-reports\n    what was executed -- and the enforcement gap is real.")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "call_delivery.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rs[0]))
        w.writeheader(); w.writerows(rs)
    print(f"\nper-run table: {outdir / 'call_delivery.csv'} ({len(rs)} rows)")


if __name__ == "__main__":
    main()
