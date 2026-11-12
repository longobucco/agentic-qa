"""F1 (EXPERIMENTS.md X15): compact text dossier of one run for hand adjudication: instruction,
checker, verdict, numbered actions, agent messages, and the first differences between the saved
result files and the gold files (the auditor may see the gold; the offline verifier never does).

    python f1_dossier.py <selection.json> <out_dir>
"""
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E   # noqa: E402


def actions(conv):
    out, msgs, step = [], [], 0
    for line in open(conv, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        it = d.get("item") or {}
        if d.get("type") != "item.completed":
            continue
        if it.get("type") == "mcp_tool_call":
            step += 1
            a = it.get("arguments") or {}
            res = it.get("result") or {}
            txt = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")[:80]
            err = (it.get("error") or "")
            out.append(f"{step:3} {it.get('tool')} {json.dumps(a, ensure_ascii=False)[:150]}"
                       + (f"  -> {txt}" if txt and txt != "Success" else "") + (f"  ERROR {str(err)[:80]}" if err else ""))
        elif it.get("type") == "agent_message":
            msgs.append(f"[after step {step}] {it.get('text', '')[:600]}")
        elif it.get("type") == "command_execution":
            step += 1
            out.append(f"{step:3} shell {it.get('command', '')[:150]}")
    return out, msgs


def gold_diff(run_dir):
    ev = json.loads((run_dir / "eval.json").read_text())
    arts = {a["name"]: a for a in ev.get("eval_artifacts", []) if a.get("kept")}
    gold = [n for n, a in arts.items() if a.get("is_gold")]
    res = [n for n, a in arts.items() if not a.get("is_gold")]
    lines = []
    for r in res:
        g = next((x for x in gold if Path(x).suffix == Path(r).suffix), None)
        if not g:
            continue
        a = E.render_file(run_dir / "eval_artifacts" / r).splitlines()
        b = E.render_file(run_dir / "eval_artifacts" / g).splitlines()
        d = [x for x in difflib.unified_diff(a, b, "result", "gold", n=0, lineterm="") if not x.startswith("@@")]
        lines.append(f"--- diff {r} vs {g}: {len(d)} lines" + ("\n" + "\n".join(d[:30]) if d else " (identical rendering)"))
    return "\n".join(lines) or "(no result/gold file pair saved)"


def _where(rd):
    """Run location for the dossier header. A run of the frozen F1 sample can live on the external
    archive, outside the local results tree, so relative_to() cannot be assumed."""
    try:
        return rd.relative_to(E.RESULTS)
    except ValueError:
        return Path(*rd.parts[-3:])          # tree/task/run_k, wherever it is mounted


def main(sel, out):
    tasks = E.tasks()
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    for s in json.loads(Path(sel).read_text()):
        rd = Path(s["eval"]).resolve().parent
        ev = json.loads((rd / "eval.json").read_text()); r = json.loads((rd / "result.json").read_text())
        t = tasks[s["task"]]
        acts, msgs = actions(rd / "conversation.jsonl") if (rd / "conversation.jsonl").exists() else ([], [])
        txt = "\n".join([
            f"# {s['task'][:8]} {s['arm']} ({s['label']}) {t['snapshot']} — {_where(rd)}",
            f"INSTRUCTION: {t['instruction']}",
            f"CHECKER: {json.dumps(t['evaluator'].get('func'))} options {json.dumps(t['evaluator'].get('options', ''))[:200]}",
            f"VERDICT: {ev['verdict']} {ev.get('reason', '')[:150]} | answer {(r.get('answer') or '')[:10]} | steps {r.get('agent_steps_used')}",
            "MESSAGES:", *msgs, "ACTIONS:", *acts[:70], *(["... (%d more)" % (len(acts) - 70)] if len(acts) > 70 else []),
            "RESULT vs GOLD:", gold_diff(rd)])
        (out / f"{s['task'][:8]}{s['arm']}.txt").write_text(txt)
    print(len(list(out.glob('*.txt'))), "dossiers ->", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
