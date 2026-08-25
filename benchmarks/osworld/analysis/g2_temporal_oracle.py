"""G2 (temporal half): does the oracle depend on state that can change without us touching
anything? Offline, no sandbox -- static fingerprint of the 369 task JSON.

Two distinct risks:
  setup risk  - a config "download" step fetches a file by URL. If it 404s (observed for
                real, see README) or changes, task setup breaks -> ENVIRONMENT_ERROR.
                Doesn't touch the oracle itself.
  oracle risk - an evaluator's EXPECTED-side getter queries a live third-party service at
                scoring time. If that service's content changes, ground truth drifts -- a
                PASS today could be a FAIL next month with zero code change on our side.

EXTERNAL_LIVE_GETTERS (cloud_file, googledrive_file, info_from_website, pdf_from_url,
gotoRecreationPage_and_get_html_content) lives in env/osworld_eval.py, the module that
resolves getters, and is imported from here rather than duplicated. cloud_file is 214/369
tasks, 100% pointing at huggingface.co. Two more live getters exist in desktop_env
(number_of_search_results, macys_product_url_parse) but don't occur in this task set.

Run: python -m benchmarks.osworld.analysis.g2_temporal_oracle
"""
import collections
import json
from urllib.parse import urlparse

from benchmarks.osworld import config
from benchmarks.osworld.env.osworld_eval import EXTERNAL_LIVE_GETTERS, _specs
from core.results import is_run_dir


def download_urls(task):
    urls = []
    for step in task.get("config", []) or []:
        if step.get("type") != "download":
            continue
        for f in step.get("parameters", {}).get("files", []) or []:
            if f.get("url"):
                urls.append(f["url"])
    return urls


def oracle_dependency(task):
    """'expected' getters only -- the ground-truth side. Returns the set of
    external-live getter types used, empty if none (oracle is self-contained)."""
    ev = task.get("evaluator", {}) or {}
    types = {s["type"] for s in _specs(ev, "expected") if s and s.get("type")}
    return types & EXTERNAL_LIVE_GETTERS


def result_dependency(task):
    """'result' getters that are external-live -- reads the agent's own outcome via a
    third-party API. Eval-pipeline fragility, not oracle drift -- tracked separately."""
    ev = task.get("evaluator", {}) or {}
    types = {s["type"] for s in _specs(ev, "result") if s and s.get("type")}
    return types & EXTERNAL_LIVE_GETTERS


def is_hermetic(task):
    """True when scoring this task reads nothing outside the guest we provisioned.

    On a web-dependent task, a verdict that changes between runs confounds agent instability
    with the gold moving underneath us -- G3's clean RQ1 number needs hermetic tasks only.
    Setup-side downloads don't count: a failed one surfaces as ENVIRONMENT_ERROR (a separate,
    already-excluded outcome), not a silent drift of the verdict."""
    return not (oracle_dependency(task) or result_dependency(task))


def hermeticity_split(tasks_path=None):
    tasks = []
    with open(tasks_path or config.TASKS_FILE) as f:
        for line in f:
            tasks.append(json.loads(line))
    hermetic = [t for t in tasks if is_hermetic(t)]
    return {"hermetic": hermetic, "web_dependent": [t for t in tasks if not is_hermetic(t)]}


def oracle_drift_report(results_dir, system):
    """Did any task's gold reference change between runs? Reads the `gold_sha256` map each run
    records (see osworld_eval.hash_gold_artifacts) and compares across a task's runs.

      stable  - identical gold every run -> variance is attributable to the agent, belongs in
                the RQ1 estimate even though the oracle is fetched over the network
      drifted - gold changed mid-campaign -> ORACLE_DRIFT, exclude those runs from agent-
                instability numbers
      single_run - only one run, nothing to compare yet
      no_gold - hermetic task, or gold never fetched
    """
    base = results_dir / system
    out = {"stable": [], "drifted": [], "single_run": [], "no_gold": []}
    if not base.exists():
        return out
    for tdir in sorted(p for p in base.iterdir() if p.is_dir()):
        seen = []
        for rdir in sorted(p for p in tdir.iterdir()
                            if p.is_dir() and is_run_dir(p.name)):
            ev = rdir / "eval.json"
            if not ev.exists():
                continue
            try:
                gold = json.loads(ev.read_text()).get("gold_sha256") or {}
            except Exception:
                continue
            if gold:
                seen.append((rdir.name, gold))
        if not seen:
            out["no_gold"].append(tdir.name)
        elif len(seen) == 1:
            out["single_run"].append(tdir.name)
        elif all(g == seen[0][1] for _, g in seen):
            out["stable"].append(tdir.name)
        else:
            changed = sorted({f for _, g in seen for f in g
                              if any(g2.get(f) != g.get(f) for _, g2 in seen)})
            out["drifted"].append({"task": tdir.name, "files": changed,
                                    "per_run": {r: g for r, g in seen}})
    return out


def audit(tasks_path=None):
    tasks = []
    with open(tasks_path or config.TASKS_FILE) as f:
        for line in f:
            tasks.append(json.loads(line))

    domain_counts = collections.Counter()
    setup_risk_tasks = []
    for t in tasks:
        urls = download_urls(t)
        if urls:
            setup_risk_tasks.append(t["id"])
        for u in urls:
            domain_counts[urlparse(u).netloc] += 1

    oracle_risk_tasks = [(t["id"], oracle_dependency(t)) for t in tasks if oracle_dependency(t)]
    result_risk_tasks = [(t["id"], result_dependency(t)) for t in tasks if result_dependency(t)]

    return {
        "n_tasks": len(tasks),
        "setup_risk": {"n_tasks": len(setup_risk_tasks), "task_ids": setup_risk_tasks,
                        "url_domains": dict(domain_counts.most_common())},
        "oracle_risk": {"n_tasks": len(oracle_risk_tasks), "tasks": oracle_risk_tasks},
        "result_risk": {"n_tasks": len(result_risk_tasks), "tasks": result_risk_tasks},
    }


def _main():
    a = audit()
    n = a["n_tasks"]
    print(f"tasks: {n}\n")

    sr = a["setup_risk"]
    print(f"SETUP risk (config download step, breaks if the URL 404s/changes): "
          f"{sr['n_tasks']}/{n} ({100*sr['n_tasks']/n:.1f}%)")
    print("  url domains:")
    for dom, c in sr["url_domains"].items():
        print(f"    {c:>4}  {dom}")

    def _breakdown(label, risk):
        import collections
        type_counts = collections.Counter()
        for _, types in risk["tasks"]:
            for t in types:
                type_counts[t] += 1
        print(f"\n{label}: {risk['n_tasks']}/{n} ({100*risk['n_tasks']/n:.1f}%)")
        for t, c in type_counts.most_common():
            print(f"    {c:>4}  {t}")
        sample = risk["tasks"][:3]
        if sample:
            print("  sample task ids:")
            for tid, types in sample:
                print(f"    {tid}  {sorted(types)}")

    _breakdown("ORACLE risk (expected-side getter queries a live external "
               "service -- ground truth can drift)", a["oracle_risk"])
    _breakdown("RESULT risk (result-side getter queries a live external service "
               "-- eval-pipeline fragility, not oracle drift)", a["result_risk"])

    print(f"\nnote: a real 404 on a config download step was observed live during "
          f"harness bring-up (see README) -- setup risk is not hypothetical.")


if __name__ == "__main__":
    _main()
