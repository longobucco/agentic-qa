"""X17 (EXPERIMENTS.md): the offline verifier E3 (b) as a selector among a task's existing runs.

    python x17_selector.py judge [-j N] [--set classF]   judge every run of the 5-run trees (cached)
    python x17_selector.py report [--set classF]         selected vs random (pass@1) vs oracle

E3 is exactly the frozen variant (b) of X13 (`e3_verifier.judge`, criteria from results/_e3/criteria_b).
Selection per task: among its runs judged *done*, one at random (seeded); none done: a random run.
Expected values are computed exactly over the random choice, not by sampling one draw.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e3_verifier as E   # noqa: E402

OUT = E.RESULTS / "_e3" / "x17_b"
ARMS = {"sonnet": ("agent_computer_sonnet5_effortmax_pkgfix3", ("", "_e1card", "_e1card2", "_code")),
        "astra": ("agent_computer_gpt6astra_max_codex01534_pkgfix3", ("", "_e1card", "_e1card2", "_code"))}


def sets():
    return json.loads((E.OSW / "data" / "phase3_sets.json").read_text())["sets"]


def rows(set_name):
    for model, (pre, arms) in ARMS.items():
        for arm in arms:
            for t in sets()[set_name]["tasks"]:
                for f in sorted(glob.glob(f"{E.RESULTS}/{pre}{arm}_official/{t}/run_*/eval.json")):
                    v = json.load(open(f))["verdict"]
                    if v in ("SUCCESS", "FAILURE"):
                        yield {"model": model, "arm": arm.strip("_") or "a0", "task": t, "verdict": v,
                               "run": str(Path(f).parent.relative_to(E.RESULTS))}


def path(row):
    return OUT / row["run"].replace("/", "__") / "e3.json"


def judge_one(row, tasks):
    out = path(row)
    if out.exists():
        return
    E.judge("b", row, tasks[row["task"]], out=out)


def cmd_judge(a):
    tasks = E.tasks()
    rs = list(rows(a.set))
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for i, _ in enumerate(ex.map(lambda r: judge_one(r, tasks), rs), 1):
            if i % 25 == 0:
                print(f"x17 judge: {i}/{len(rs)}", flush=True)
    print(f"x17 judge: {sum(path(r).exists() for r in rs)}/{len(rs)} runs judged")


def _boot(per_task, n=2000, seed=0):
    rng, keys, vals = random.Random(seed), list(per_task), []
    for _ in range(n):
        s = [per_task[rng.choice(keys)] for _ in keys]
        vals.append(sum(x for x in s) / len(s))
    vals.sort()
    return vals[int(0.025 * n)], vals[int(0.975 * n) - 1]


def cmd_report(a):
    rs = list(rows(a.set))
    groups = {}
    for r in rs:
        p = path(r)
        r["e3"] = json.loads(p.read_text())["e3"] if p.exists() else "missing"
        groups.setdefault((r["model"], r["arm"]), {}).setdefault(r["task"], []).append(r)
    lines = []
    for (model, arm), by_task in sorted(groups.items()):
        sel, rnd, orc, diff, abst, miss = {}, {}, {}, {}, 0, 0
        for t, runs in by_task.items():
            s = [x["verdict"] == "SUCCESS" for x in runs]
            done = [x["verdict"] == "SUCCESS" for x in runs if x["e3"] == "done"]
            miss += sum(x["e3"] == "missing" for x in runs)
            rnd[t] = sum(s) / len(s)
            sel[t] = sum(done) / len(done) if done else rnd[t]
            abst += not done
            orc[t] = float(any(s))
            diff[t] = sel[t] - rnd[t]
        n = len(by_task)
        m = lambda d: sum(d.values()) / n
        lo, hi = _boot(diff)
        lines.append(f"{model:6} {arm:7} tasks {n:2} runs {sum(map(len, by_task.values())):3} | random pass@1 {m(rnd):.1%} "
                     f"| selected {m(sel):.1%} | oracle {m(orc):.1%} | selected-random {m(diff):+.1%} (95% CI {lo:+.1%} to {hi:+.1%}) "
                     f"| tasks with no done {abst} | runs not judged {miss}")
    txt = "\n".join(lines)
    print(txt)
    (HERE / "tables" / f"x17_{a.set}.txt").write_text(txt + "\n")


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    j = s.add_parser("judge"); j.add_argument("-j", "--jobs", type=int, default=4); j.add_argument("--set", default="classF")
    r = s.add_parser("report"); r.add_argument("--set", default="classF")
    a = p.parse_args()
    {"judge": cmd_judge, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
