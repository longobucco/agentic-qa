"""Load the repo-root `.env` so EVERY benchmark run shares the same keys (DAYTONA, OPENAI, ...).

The `.env` lives at the repo root (one place for all benchmarks). `core.run.main` loads it at
startup, and `benchmarks/webarena/env/daytona.py` loads it for its standalone CLI — so a runner's
preflight (`OPENAI_API_KEY` for alumnium/colorbrowser) and the Daytona client both see the keys
without anyone needing to `source .env` first.

`setdefault` semantics: anything already exported in the environment WINS over the file, so CI /
explicit `KEY=… python -m …` overrides still take precedence.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # core/ is one level under the repo root
ENV_FILE = ROOT / ".env"


def load_dotenv(path=None):
    """Read KEY=VALUE lines from the root .env into os.environ (setdefault). No-op if absent."""
    p = Path(path) if path else ENV_FILE
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
