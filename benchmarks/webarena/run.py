"""WebArena entry point. Orchestration lives in `core.run`; this just wires it.

Requires a provisioned stack (set WA_*_URL) — see benchmarks/webarena/README.md. Local on
this arm64 Mac the stack can't run; provision it on an x86 host (e.g. a Daytona sandbox).

  python -m benchmarks.webarena.data.download_data        # once: vendor task configs
  WA_REDDIT_URL=http://localhost:9999 python -m benchmarks.webarena.run --per-bucket 1
  python -m benchmarks.webarena.run --ids webarena-27 --dry-run
"""
from benchmarks.webarena.benchmark import build
from core.run import main

if __name__ == "__main__":
    main(build())
