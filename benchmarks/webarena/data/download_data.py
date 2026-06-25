"""Vendor the WebArena task configs into a local jsonl.

WebArena ships its tasks as `config_files/test.raw.json` in the upstream repo (intent,
start_url with __SITE__ placeholders, sites, and the deterministic `eval` spec). We fetch it
and normalize to one line per task:

    {"id": "webarena-27", "intent": "...", "start_url": "__REDDIT__/...",
     "sites": ["reddit"], "eval": { ...string_match|url_match|program_html... }}

`tasks.py` templates the placeholders to your hosted URLs at load time. Run once:
    python -m benchmarks.webarena.data.download_data
"""
import json
import urllib.request
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/web-arena-x/webarena/main/config_files/test.raw.json"
OUT = Path(__file__).resolve().parent / "webarena.jsonl"


def main():
    print(f"downloading {RAW_URL} ...")
    raw = json.loads(urllib.request.urlopen(RAW_URL, timeout=60).read().decode())
    print(f"{len(raw)} tasks")
    n = 0
    with OUT.open("w") as f:
        for t in raw:
            f.write(json.dumps({
                "id": f"webarena-{t['task_id']}",
                "intent": t["intent"],
                "start_url": t.get("start_url", ""),
                "sites": t.get("sites", []),
                "require_login": t.get("require_login", False),
                "eval": t["eval"],
            }) + "\n")
            n += 1
    print(f"wrote {n} tasks -> {OUT}")
    by_site = {}
    for line in OUT.read_text().splitlines():
        for s in json.loads(line).get("sites", []):
            by_site[s] = by_site.get(s, 0) + 1
    print("tasks per site:", dict(sorted(by_site.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
