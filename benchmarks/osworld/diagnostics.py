"""Per-run diagnostic manifest: what was collected, what was not, and why. Zero quota.

    .venv/bin/python -m benchmarks.osworld.diagnostics [--write] [--tree NAME] [--limit N]

The DEV1 checkpoint has to answer "how many runs are reconstructible, and why the others are not".
Today that question needs a person to look in a directory and guess. This writes the answer down,
one `diagnostics.json` per run, keyed so every ledger can be joined on it.

Deliberately **post hoc**: it reads a finished run directory and writes one file next to it. It
never touches the guest, never runs before the evaluator, and cannot alter what the model saw or
what the official score was -- the strongest available guarantee that adding diagnostics does not
perturb the thing being diagnosed. The one piece that genuinely cannot be reconstructed afterwards,
*why the evaluator ended up with no artefact*, is recorded during scoring instead
(`runners/common._artefact_capture_diagnosis`, B7).

`run_key` is deterministic -- sha256(tree | task | run)[:16] -- so it can be computed for the
1,553 runs already on disk as well as for new ones, and the existing ledgers can be re-keyed
without re-running anything.

A run is `reconstructible` when its trajectory can be replayed: a transcript that parses to at
least one tool call, with at least one screenshot in it. Everything else is reported as a named
gap rather than as a missing file.
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "campaign_analysis"))

RESULTS = HERE / "results"
MANIFEST_NAME = "diagnostics.json"


def run_key(tree, task_id, run_name):
    return hashlib.sha256(f"{tree}|{task_id}|{run_name}".encode()).hexdigest()[:16]


def _file(path, *, why_absent):
    if path.exists():
        try:
            size = path.stat().st_size
        except OSError as e:
            return {"present": False, "reason": f"{type(e).__name__}: {e}"}
        return {"present": True, "bytes": size,
                **({"empty": True, "reason": "file exists but is empty"} if size == 0 else {})}
    return {"present": False, "reason": why_absent}


def _json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def collect(run_dir, tree=None):
    """The diagnostic manifest of one finished run. Never raises on a malformed run."""
    run_dir = Path(run_dir)
    tree = tree or run_dir.parents[1].name
    task = run_dir.parent.name
    result = _json(run_dir / "result.json") or {}
    ev = _json(run_dir / "eval.json")
    infra = (run_dir / "infra_error.json").exists()

    transcript = _file(run_dir / "conversation.jsonl",
                       why_absent="the agent CLI produced no event log, or it was never saved")
    calls = shots = 0
    if transcript.get("present"):
        try:
            from transcripts import calls as _calls
            for c in _calls(run_dir):
                calls += 1
                shots += bool(c.get("image"))
        except Exception as e:
            transcript["parse_error"] = f"{type(e).__name__}: {e}"
    transcript["tool_calls"] = calls
    transcript["screenshots"] = shots

    rec = {
        "run_key": run_key(tree, task, run_dir.name),
        "tree": tree, "task": task, "run": run_dir.name,
        "verdict": (ev or {}).get("verdict"),
        "scored": ev is not None,
        "infra_error_log": infra,
        "collected": {
            "transcript": transcript,
            # Codex keeps the real trajectory in its rollout, under CODEX_HOME: the saved
            # transcript is the event log, not the trajectory (measured 12/11: 252 characters of
            # text against a 3 MB rollout). `archive_rollouts` copies it in. A run from before
            # that existed records nothing, which is not the same as a runtime that has no
            # rollout at all -- say which.
            "rollout_archive": ({"present": bool(result.get("rollout_archived")),
                                 "files": result.get("rollout_files"),
                                 "bytes": result.get("rollout_bytes"),
                                 **({} if result.get("rollout_archived")
                                    else {"reason": result.get("rollout_error")
                                          or "archiving reported failure"})}
                                if result.get("rollout_archived") is not None
                                else {"present": False,
                                      "reason": ("not recorded: a Claude Code run, which has no "
                                                 "rollout, or an Astra run from before rollout "
                                                 "archiving existed")}),
            "final_screenshot": _file(run_dir / "final.png",
                                      why_absent="never captured: the run ended before the "
                                                 "final-state capture, or the guest was gone"),
            "agent_report": _file(run_dir / "agent_output.txt",
                                  why_absent="the agent produced no final text"),
            "cli_stderr": _file(run_dir / "codex_stderr.txt",
                                why_absent="the CLI wrote nothing to stderr (normal)"),
            "mcp_state": _file(run_dir / "mcp_state.json",
                               why_absent="the MCP server never started: the agent had no "
                                          "`computer` tool"),
            "tool_audit": _file(run_dir / "tool_audit.json",
                                why_absent="no audit written: the transcript was unavailable"),
            "final_state_manifest": _file(run_dir / "final_state" / "manifest.json",
                                          why_absent="no final-state capture"),
            "eval_artifacts_dir": _file(run_dir / "eval_artifacts",
                                        why_absent="the evaluator kept no file -- see "
                                                   "artefact_capture for why"),
        },
        "artefact_capture": (ev or {}).get("artefact_capture") or {
            "reason": "not recorded: this run predates the artefact-capture diagnosis"},
        "eval_state_capture": (result.get("eval_state") or {}).get("capture_status")
        if isinstance(result.get("eval_state"), dict) else
        ("absent" if result.get("eval_state") is None else "present"),
        "phase_seconds": result.get("phase_seconds") or {
            "reason": "not recorded: this run predates phase timing"},
        "steps_used": result.get("agent_steps_used"),
        "step_counter_resets": result.get("agent_step_counter_resets"),
    }

    # A null-agent run has no agent and therefore no trajectory: that is the control working as
    # designed, not a collection gap, and counting it as one hid the real coverage on the first
    # pass (0 of 350 "missing", against 99.3% coverage on the runs that had an agent).
    rec["has_agent"] = "nullagent" not in tree
    gaps = []
    if not rec["has_agent"]:
        rec["gaps"] = []
        rec["reconstructible"] = None
        rec["reconstructible_reason"] = "null agent: no trajectory exists by design"
        return rec
    if not transcript.get("present"):
        gaps.append("no transcript")
    elif calls == 0:
        gaps.append("transcript has no tool call")
    elif shots == 0:
        gaps.append("transcript has no screenshot")
    if not rec["collected"]["final_screenshot"]["present"]:
        gaps.append("no final screenshot")
    if ev is not None and not rec["collected"]["eval_artifacts_dir"]["present"]:
        gaps.append("no evaluator artefact")
    rec["gaps"] = gaps
    rec["reconstructible"] = transcript.get("present", False) and calls > 0 and shots > 0
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help=f"write {MANIFEST_NAME} into each run directory")
    ap.add_argument("--tree", default=None, help="only this results tree")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=str(HERE / "campaign_analysis" / "tables"))
    a = ap.parse_args(argv)

    pattern = f"{a.tree}/*/run_*" if a.tree else "agent_computer_*_official/*/run_*"
    dirs = sorted(p for p in RESULTS.glob(pattern) if p.is_dir())
    if a.limit:
        dirs = dirs[:a.limit]
    rows, recs = [], []
    for d in dirs:
        r = collect(d)
        recs.append(r)
        if a.write:
            (d / MANIFEST_NAME).write_text(json.dumps(r, indent=2) + "\n")
        rows.append({"run_key": r["run_key"], "tree": r["tree"], "task": r["task"][:8],
                     "run": r["run"], "verdict": r["verdict"],
                     "reconstructible": r["reconstructible"],
                     "tool_calls": r["collected"]["transcript"].get("tool_calls"),
                     "screenshots": r["collected"]["transcript"].get("screenshots"),
                     "gaps": "|".join(r["gaps"]),
                     "artefact_reason": (r["artefact_capture"] or {}).get("reason", "")[:80]})

    n = len(recs)
    agent = [r for r in recs if r["has_agent"]]
    scored = [r for r in agent if r["scored"]]
    ok = sum(1 for r in scored if r["reconstructible"])
    print(f"diagnostics — {n} run directories ({len(agent)} with an agent, "
          f"{len(scored)} of those judged)\n")
    print(f"  reconstructible, among JUDGED runs with an agent: {ok}/{len(scored)} "
          f"({100*ok/max(1, len(scored)):.1f}%)")
    print("  (a null-agent run has no trajectory by design and is not counted as a gap)\n")
    print("  why the others are not:")
    for gap, k in Counter(g for r in scored if not r["reconstructible"]
                          for g in r["gaps"]).most_common():
        print(f"    {k:>5}  {gap}")
    print("\n  by runtime:")
    for label, sel in (("astra", [r for r in scored if "gpt6astra" in r["tree"]]),
                       ("sonnet", [r for r in scored if "sonnet5" in r["tree"]])):
        got = sum(1 for r in sel if r["reconstructible"])
        if sel:
            print(f"    {label:7} {got}/{len(sel)} ({100*got/len(sel):.1f}%)")
    print("\n  collection coverage, over runs with an agent:")
    recs_c = agent
    keys = ["transcript", "final_screenshot", "rollout_archive", "mcp_state", "tool_audit",
            "final_state_manifest", "eval_artifacts_dir", "agent_report"]
    for k in keys:
        have = sum(1 for r in recs_c if r["collected"][k].get("present"))
        print(f"    {have:>5}/{len(recs_c)}  {k}")
    noart = [r for r in scored if not r["collected"]["eval_artifacts_dir"]["present"]]
    print(f"\n  scored runs with no evaluator artefact: {len(noart)}/{len(scored)}")
    for reason, k in Counter((r["artefact_capture"] or {}).get("reason", "?")
                             for r in noart).most_common(5):
        print(f"    {k:>5}  {reason[:100]}")

    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / "diagnostics_coverage.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print(f"\ntable: {outdir / 'diagnostics_coverage.csv'}")
    if not a.write:
        print(f"(dry run: pass --write to drop {MANIFEST_NAME} into each run directory)")


if __name__ == "__main__":
    main()
