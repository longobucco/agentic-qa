"""G-1 exit criterion: can every costly-phase deliverable be derived from the RAW records alone?

Answers that before a full campaign, while a missing field costs 4 runs to discover instead
of 300 (gap-research-plan.md §4, G-1). Reads only what's on disk under results/ -- no
sandbox, no re-scoring, no network. A missing field means it has to be added to the capture
before G3, not after.

Checks, one per deliverable:
  G3  reliability   -- per-task verdict across runs (modal/pass@1/flaky) + agent variance
  G3  cost governance -- $ and tokens per run, so a budget is set from data, not guessed
  G3  oracle drift  -- gold sha256 stable across runs (see g2_temporal_oracle)
  G3  infra split   -- INFRA_FLAKE/HARNESS_ERROR separable from benchmark instability (§3.4)
  G4  re-derivation -- pass-rate recomputable under both ENVIRONMENT_ERROR conventions
  §3.1 provenance   -- each record self-describing (image digest, task hash, timestamps)

Run: python -m benchmarks.osworld.analysis.g1_pilot_check
"""
import collections
import json

from benchmarks.osworld import config
from benchmarks.osworld.analysis import g2_temporal_oracle as g2t
from core.results import is_run_dir


def _load(results_dir, system):
    """{task_id: [(run_idx, result_json, eval_json), ...]} straight off disk."""
    base = results_dir / system
    out = {}
    if not base.exists():
        return out
    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        runs = []
        for rdir in sorted(p for p in tdir.iterdir()
                            if p.is_dir() and is_run_dir(p.name)):
            res, ev = rdir / "result.json", rdir / "eval.json"
            if not ev.exists():
                continue
            runs.append((
                rdir.name,
                json.loads(res.read_text()) if res.exists() else {},
                json.loads(ev.read_text()),
            ))
        if runs:
            out[tdir.name] = runs
    return out


def check(results_dir=None, system=None):
    results_dir = results_dir or config.RESULTS_DIR
    system = config.resolve_system(system)
    data = _load(results_dir, system)
    missing = []
    report = {"n_tasks": len(data), "n_runs": sum(len(v) for v in data.values())}

    # --- G3: per-task reliability (needs a verdict per run) ---
    reliability = {}
    for tid, runs in data.items():
        verdicts = [e.get("verdict") for _, _, e in runs]
        scored = [v for v in verdicts if v not in ("ENVIRONMENT_ERROR", "EVAL_ERROR")]
        modal = collections.Counter(scored).most_common(1)[0][0] if scored else None
        reliability[tid] = {
            "verdicts": verdicts,
            "modal": modal,
            "pass_at_1": (sum(v == "SUCCESS" for v in scored) / len(scored)) if scored else None,
            "flaky": len(set(scored)) > 1,
        }
    report["reliability"] = reliability

    # --- G3: cost governance (needs $ + tokens per run) ---
    # ENVIRONMENT_ERROR never invokes the agent, so it carries no cost by construction --
    # checked here, not flagged as a missing field. EVAL_ERROR is different: the agent DID
    # run (telemetry is written before scoring is attempted), so its cost is still expected.
    costs, toks = [], []
    for tid, runs in data.items():
        for rname, res, ev in runs:
            if ev.get("verdict") == "ENVIRONMENT_ERROR":
                continue
            c, i, o = (res.get("agent_cost_usd"), res.get("agent_input_tokens"),
                        res.get("agent_output_tokens"))
            if c is None:
                missing.append(f"{tid}/{rname}: agent_cost_usd")
            else:
                costs.append(c)
            if i is None or o is None:
                missing.append(f"{tid}/{rname}: token counts")
            else:
                toks.append((i, o))
    report["cost"] = {
        "runs_with_cost": len(costs),
        "total_usd": round(sum(costs), 4) if costs else None,
        "mean_usd_per_run": round(sum(costs) / len(costs), 4) if costs else None,
        "total_input_tokens": sum(i for i, _ in toks) if toks else None,
        "total_output_tokens": sum(o for _, o in toks) if toks else None,
    }

    # --- G3: oracle drift (needs gold_sha256 per run) ---
    report["oracle_drift"] = g2t.oracle_drift_report(results_dir, system)

    # --- G3: infra split (§3.4) ---
    from core import reporting
    report["infra_errors"] = reporting.collect_infra_errors(results_dir, system)

    # --- G4: re-derive pass-rate under BOTH env-error conventions, from raw only ---
    # EVAL_ERROR dropped first: it's our own scoring-pipeline flakiness, not the upstream
    # ENVIRONMENT_ERROR convention question G4 is about -- neither convention should count it.
    all_v = [e.get("verdict") for runs in data.values() for _, _, e in runs
             if e.get("verdict") != "EVAL_ERROR"]
    n_env = sum(v == "ENVIRONMENT_ERROR" for v in all_v)
    n_ok = sum(v == "SUCCESS" for v in all_v)
    excl_den = len(all_v) - n_env
    report["g4_scissor"] = {
        "excluding_env_errors": round(n_ok / excl_den, 4) if excl_den else None,
        "counting_env_as_failure": round(n_ok / len(all_v), 4) if all_v else None,
        "n_env_errors": n_env,
    }

    # --- §3.1 provenance ---
    for tid, runs in data.items():
        for rname, res, _ in runs:
            prov = res.get("provenance") or {}
            for field in ("image", "task_sha256", "started_at", "finished_at"):
                if not prov.get(field):
                    missing.append(f"{tid}/{rname}: provenance.{field}")
    report["missing_fields"] = missing
    return report


def _main():
    r = check(system=config.system_from_argv())
    if not r["n_runs"]:
        print("No runs on disk yet — run the pilot first.")
        return
    print(f"raw records read: {r['n_runs']} run(s) across {r['n_tasks']} task(s)\n")

    print("G3 reliability (per task, from raw):")
    for tid, v in r["reliability"].items():
        print(f"  {tid[:20]:<22}{str(v['verdicts']):<34}modal={v['modal']} "
              f"pass@1={v['pass_at_1']} flaky={v['flaky']}")

    c = r["cost"]
    print(f"\nG3 cost governance: {c['runs_with_cost']}/{r['n_runs']} runs carry cost; "
          f"total ${c['total_usd']}, mean ${c['mean_usd_per_run']}/run; "
          f"tokens in={c['total_input_tokens']} out={c['total_output_tokens']}")

    d = r["oracle_drift"]
    print(f"\nG3 oracle drift: stable={len(d['stable'])} drifted={len(d['drifted'])} "
          f"single_run={len(d['single_run'])} no_gold={len(d['no_gold'])}")
    for item in d["drifted"]:
        print(f"    DRIFTED {item['task']}: {item['files']}")

    print(f"\nG3 infra split: {len(r['infra_errors'])} run-dir(s) with infra failures "
          f"(separable from benchmark instability)")

    g4 = r["g4_scissor"]
    print(f"\nG4 scissor (re-derived from raw, no re-run): "
          f"excluding env-errors={g4['excluding_env_errors']} vs "
          f"counting-as-failure={g4['counting_env_as_failure']} "
          f"({g4['n_env_errors']} env-error run(s))")

    if r["missing_fields"]:
        print(f"\nEXIT CRITERION NOT MET — {len(r['missing_fields'])} missing field(s); "
              f"add to the capture BEFORE G3:")
        for m in r["missing_fields"][:20]:
            print(f"    {m}")
    else:
        print("\nEXIT CRITERION MET: every costly-phase table above was derived from the raw "
              "records alone — no sandbox reopened, no re-scoring. The run-once contract holds.")


if __name__ == "__main__":
    _main()
