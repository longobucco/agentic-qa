"""Download the WebVoyager task set + reference answers.

Defaults to Alumnium's fork (branch `alumnium`) so our numbers are comparable to their
self-reported 98.5% run (dates updated to 2026, 20 tasks restored). Override with
WV_DATA_BASE to point at the upstream WebVoyager repo instead.
"""
import os
import urllib.request

from benchmarks.webvoyager.config import DATA_DIR

BASE = os.environ.get(
    "WV_DATA_BASE",
    "https://raw.githubusercontent.com/alumnium-hq/WebVoyager/alumnium/data",
)
FILES = ["WebVoyager_data.jsonl", "reference_answer.json"]


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        url = f"{BASE}/{name}"
        dest = DATA_DIR / name
        print(f"GET {url}")
        urllib.request.urlretrieve(url, dest)
        print(f"  -> {dest} ({dest.stat().st_size} bytes)")
    n = sum(1 for _ in open(DATA_DIR / "WebVoyager_data.jsonl"))
    print(f"\n{n} tasks ready in {DATA_DIR}")


if __name__ == "__main__":
    main()
