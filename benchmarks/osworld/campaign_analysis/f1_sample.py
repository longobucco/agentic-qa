"""F1 checker-audit sample (EXPERIMENTS.md X15, user decision 5/11): per agent model, 60 FAILURE and
60 SUCCESS runs, stratified by application (task bucket) and image (original `protocol361img3`
campaign on the archive, pkgfix3 trees here). Allocation proportional to the stratum's run count
(largest remainder), at most one run per task within a stratum so the sample spreads over tasks,
fixed seed. Infeasible-by-design tasks and runs without an official verdict are left out.

    python f1_sample.py <archive_results_root> <out.json>
"""
import glob
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OSW = HERE.parent
sys.path.insert(0, str(OSW.parent.parent))
from benchmarks.osworld import tasks as T   # noqa: E402

PER = 60
SEED = 20261105
MODELS = {"sonnet": ("agent_computer_sonnet5_effortmax_protocol361img3_official", "agent_computer_sonnet5_effortmax_pkgfix3"),
          "astra": ("agent_computer_gpt6astra_max_codex01534_protocol361img3_official", "agent_computer_gpt6astra_max_codex01534_pkgfix3")}


def runs(archive, model):
    orig, pk = MODELS[model]
    trees = [("original", Path(archive) / orig)]
    trees += [("pkgfix3", Path(p)) for p in sorted(glob.glob(str(OSW / "results" / (pk + "*_official"))))
              if "nullagent" not in p]
    task_by_id = {t["id"]: t for t in T.load_tasks()}
    for image, tree in trees:
        for f in sorted(tree.glob("*/run_*/eval.json")):
            ev = json.loads(f.read_text())
            t = task_by_id.get(ev.get("id"))
            if not t or ev.get("verdict") not in ("SUCCESS", "FAILURE") or (t.get("evaluator") or {}).get("func") == "infeasible":
                continue
            yield {"model": model, "image": image, "bucket": T.bucket_of(t), "task": ev["id"],
                   "verdict": ev["verdict"], "run": str(f.parent)}


def allocate(sizes, n):
    tot = sum(sizes.values())
    raw = {k: n * v / tot for k, v in sizes.items()}
    out = {k: int(x) for k, x in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: n - sum(out.values())]:
        out[k] += 1
    return out


def main(archive, dest):
    rng = random.Random(SEED)
    sample = []
    for model in MODELS:
        pool = list(runs(archive, model))
        for verdict in ("FAILURE", "SUCCESS"):
            strata = defaultdict(list)
            for r in pool:
                if r["verdict"] == verdict:
                    strata[(r["image"], r["bucket"])].append(r)
            alloc = allocate({k: len(v) for k, v in strata.items()}, PER)
            for k in sorted(strata):
                byt = defaultdict(list)
                for r in strata[k]:
                    byt[r["task"]].append(r)
                tids = sorted(byt); rng.shuffle(tids)
                picked = [rng.choice(byt[t]) for t in tids[: alloc[k]]]
                if len(picked) < alloc[k]:   # fewer tasks than slots: fill with other runs of the stratum
                    rest = [r for r in strata[k] if r not in picked]; rng.shuffle(rest)
                    picked += rest[: alloc[k] - len(picked)]
                sample += picked
    Path(dest).write_text(json.dumps({"seed": SEED, "per_model_per_verdict": PER, "runs": sample}, indent=1))
    from collections import Counter
    print(Counter((r["model"], r["verdict"], r["image"]) for r in sample))
    print("tasks:", len({r["task"] for r in sample}), "-> ", dest)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
