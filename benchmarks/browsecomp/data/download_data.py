"""Fetch + decrypt the BrowseComp test set into a local tasks jsonl.

BrowseComp (OpenAI) ships **canary-encrypted**: each CSV row has `problem` and `answer`
base64+XOR-encrypted under a per-row `canary` password (so the plaintext Q/A isn't trivially
scrapeable / trained on). We decrypt with the published scheme: key = repeat(sha256(canary))
to the ciphertext length, then XOR. Output one line per task:

    {"id": "browsecomp-0000", "ques": "<question>", "answer": "<reference>", "bucket": "<topic>"}

`ques` is the question the agent must research; `answer` is the short reference the grader
checks against (NOT shown to the agent). Run once:  python -m benchmarks.browsecomp.data.download_data
"""
import base64
import csv
import hashlib
import io
import json
import urllib.request
from pathlib import Path

CSV_URL = "https://openaipublic.blob.core.windows.net/simple-evals/browse_comp_test_set.csv"
OUT = Path(__file__).resolve().parent / "browsecomp.jsonl"


def _derive_key(password: str, length: int) -> bytes:
    key = hashlib.sha256(password.encode()).digest()
    return (key * (length // len(key) + 1))[:length]


def decrypt(ciphertext_b64: str, password: str) -> str:
    enc = base64.b64decode(ciphertext_b64)
    key = _derive_key(password, len(enc))
    return bytes(a ^ b for a, b in zip(enc, key)).decode()


def main():
    print(f"downloading {CSV_URL} ...")
    raw = urllib.request.urlopen(CSV_URL, timeout=60).read().decode()
    rows = list(csv.DictReader(io.StringIO(raw)))
    print(f"{len(rows)} rows; columns: {list(rows[0].keys())}")

    n = 0
    with OUT.open("w") as f:
        for i, row in enumerate(rows):
            canary = row["canary"]
            ques = decrypt(row["problem"], canary)
            ref = decrypt(row["answer"], canary)
            # BrowseComp has no topic column; bucket everything together for now.
            bucket = row.get("problem_topic") or row.get("category") or "all"
            f.write(json.dumps({
                "id": f"browsecomp-{i:04d}", "ques": ques, "answer": ref, "bucket": bucket,
            }) + "\n")
            n += 1
    print(f"wrote {n} tasks -> {OUT}")
    # show one decrypted sample so a human can eyeball that decryption worked
    s = json.loads(OUT.read_text().splitlines()[0])
    print(f"\nsample id={s['id']} bucket={s['bucket']}\n  Q: {s['ques'][:160]}\n  A: {s['answer'][:120]}")


if __name__ == "__main__":
    main()
