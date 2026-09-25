"""Campaign driver for the official-protocol OSWorld-Verified campaigns (361 tasks x 5 runs).

    BACKEND=daytona|kvm ARM=sonnet|astra PARALLEL=K MAX_HOURS=H .venv/bin/python scripts/g_official361_driver.py
    ... --dry-run    # print the planned batches, run nothing

BACKEND picks the environment backend (required, read from the shell only). The rules -- rounds,
resume, backoff, stop, signals, preflight first -- live in benchmarks/osworld/campaign.py; the
backend supplies its protocol variables, its extra refusals and the sweep of its own orphans.
"""
import importlib
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:   # run as a script: make `benchmarks` / `core` importable
    sys.path.insert(0, str(_ROOT))

BACKENDS = {"daytona": "benchmarks.osworld.campaign_daytona",
            "kvm": "benchmarks.osworld.campaign_kvm"}


def main(argv=None):
    name = os.environ.get("BACKEND", "").strip()
    if name not in BACKENDS:
        print(f"BACKEND={name!r}: expected one of {', '.join(sorted(BACKENDS))}",
              file=sys.stderr)
        return 2
    from benchmarks.osworld import campaign
    return campaign.main(importlib.import_module(BACKENDS[name]), argv)


if __name__ == "__main__":
    sys.exit(main())
