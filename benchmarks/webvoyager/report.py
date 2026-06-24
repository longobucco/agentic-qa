"""Summarize results: overall success rate, per-site breakdown, and the failed tasks."""
import json

from config import RESULTS_DIR


def collect(subdir="agentbrowser"):
    base = RESULTS_DIR / subdir
    rows = []
    if not base.exists():
        return rows
    for d in sorted(base.iterdir()):
        ev = d / "eval.json"
        if ev.exists():
            try:
                r = json.loads(ev.read_text())
            except Exception:
                continue
            res = d / "result.json"
            ans = ""
            if res.exists():
                try:
                    ans = json.loads(res.read_text()).get("answer", "")
                except Exception:
                    pass
            r["answer"] = ans
            rows.append(r)
    return rows


def summarize(subdir="agentbrowser"):
    rows = collect(subdir)
    if not rows:
        print("No evaluated tasks yet.")
        return
    n = len(rows)
    ok = sum(1 for r in rows if r["verdict"] == "SUCCESS")
    print(f"=== WebVoyager [{subdir}] ===")
    print(f"Evaluated: {n}   Success: {ok}   Failure: {n-ok}   Rate: {100*ok/n:.1f}%")

    sites = {}
    for r in rows:
        site = r["id"].split("--")[0]
        s = sites.setdefault(site, [0, 0])
        s[0] += 1
        s[1] += 1 if r["verdict"] == "SUCCESS" else 0
    print("\nPer-site:")
    for site in sorted(sites):
        tot, good = sites[site]
        print(f"  {site:<16} {good}/{tot}  ({100*good/tot:.0f}%)")

    fails = [r for r in rows if r["verdict"] != "SUCCESS"]
    print(f"\nFailed tasks ({len(fails)}):")
    for r in fails:
        print(f"  - {r['id']}: {r.get('reason','')}")


if __name__ == "__main__":
    import sys
    summarize(sys.argv[1] if len(sys.argv) > 1 else "agentbrowser")
