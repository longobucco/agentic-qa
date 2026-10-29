"""Controllable / uncontrollable rubric for E3 (EXPERIMENTS.md X11).

Each Astra task's hand-written cause (astra_ledger.json "kind") is mapped to who could have
avoided the failure: the agent (controllable), nobody acting through the desktop (uncontrollable:
environment, infrastructure, checker, task wording, protocol rule), or both (mixed). Task-level
labels; X11 states the limits. Writes tables/e3_controllability.csv and prints the summary.
"""
import csv
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Settings tasks where a terminal route exists on pkgfix3 (BUG_REGISTER A1, E1): the give-up on the
# bare desktop is the agent's, not the environment's, once the image is corrected.
ROUTE_EXISTS = {"a4d98375", "f9be0997", "bedcedc4", "3ce045a0"}
# Verified unsolvable on this image whatever the agent does (BUG_REGISTER G4, G7; RAW loader absent).
UNSOLVABLE = {"ec4e3f68", "94d95f96", "dbbf4b99"}
UNCONTROLLABLE = ("ENV", "INFRA", "CHECKER", "HARNESS", "SPEC", "UNEXPLAINED")


def label(task, kind):
    if kind.startswith("NOT-A-FAILURE"):
        return "excluded", "no failure on the official infrastructure"
    parts = [p.strip() for p in kind.replace("/", "+").split("+")]
    agent = any(p.startswith("AGENT") for p in parts)
    other = any(p.startswith(UNCONTROLLABLE) for p in parts)
    if task in UNSOLVABLE:
        return "uncontrollable", "verified unsolvable on this image"
    if task in ROUTE_EXISTS:
        return "controllable", "route available on the corrected image (A1)"
    if agent and not other:
        return "controllable", "agent error with environment and checker working"
    if other and not agent:
        return "uncontrollable", "environment, infrastructure, checker, wording or protocol rule"
    return "mixed", "agent and non-agent factors together"


def main():
    ledger = json.loads((HERE / "astra_ledger.json").read_text())
    rows = [{"task": t, "bucket": v.get("bucket", ""), "kind": v.get("kind", ""),
             "label": label(t, v.get("kind", ""))[0], "why": label(t, v.get("kind", ""))[1]}
            for t, v in sorted(ledger.items())]
    with open(HERE / "tables" / "e3_controllability.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(dict(Counter(r["label"] for r in rows)))


if __name__ == "__main__":
    main()
