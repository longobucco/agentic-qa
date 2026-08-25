"""Fetch OSWorld task specs -> osworld_<release>.jsonl.
Confirm OSW_RAW_BASE / OSW_INDEX for the release you use.

  OSW_RELEASE=verified python -m benchmarks.osworld.data.download_data
"""
import json
import os
import urllib.request
from pathlib import Path

UPSTREAM_COMMIT = "091f5ef1d5544bc74953c77875d5feb5bed30108"  # xlang-ai/OSWorld main, 2026-07-28

# "v2"/OSWorld 2.0 is a different benchmark (xlang-ai/OSWorld-V2): different schema, own
# desktop_env. Not integrated -- refuse rather than silently parse it wrong.
SUPPORTED_RELEASES = {"verified"}

RELEASE = os.environ.get("OSW_RELEASE", "verified").strip().lower()
if RELEASE not in SUPPORTED_RELEASES and not os.environ.get("OSW_RAW_BASE"):
    raise SystemExit(
        f"OSW_RELEASE={RELEASE!r} not wired up (only {sorted(SUPPORTED_RELEASES)} are). "
        f"Set OSW_RAW_BASE and OSW_INDEX explicitly if you've ported the parser yourself."
    )

RAW_BASE = os.environ.get(
    "OSW_RAW_BASE",
    f"https://raw.githubusercontent.com/xlang-ai/OSWorld/{UPSTREAM_COMMIT}/evaluation_examples",
)
INDEX = os.environ.get("OSW_INDEX", "test_all.json")     # {app: [task_id, ...]}
OUT = Path(__file__).resolve().parent / f"osworld_{RELEASE}.jsonl"


def _get_json(url):
    return json.loads(urllib.request.urlopen(url, timeout=60).read().decode())


def main():
    index_url = f"{RAW_BASE}/{INDEX}"
    print(f"downloading index {index_url} (release={RELEASE}) ...")
    index = _get_json(index_url)
    n, skipped = 0, 0
    with OUT.open("w") as f:
        for app, ids in index.items():
            for tid in ids:
                try:
                    t = _get_json(f"{RAW_BASE}/examples/{app}/{tid}.json")
                except Exception as e:
                    print(f"  skip {app}/{tid}: {e}")
                    skipped += 1
                    continue
                f.write(json.dumps({
                    "id": t.get("id", tid),
                    "instruction": t.get("instruction", ""),
                    "related_apps": t.get("related_apps", [app]),
                    "config": t.get("config", []),
                    "evaluator": t.get("evaluator", {}),
                    "snapshot": t.get("snapshot", app),
                }) + "\n")
                n += 1
    print(f"wrote {n} tasks ({skipped} skipped) -> {OUT}")
    by_app = {}
    for line in OUT.read_text().splitlines():
        for a in json.loads(line).get("related_apps", []):
            by_app[a] = by_app.get(a, 0) + 1
    print("tasks per app:", dict(sorted(by_app.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
