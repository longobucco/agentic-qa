"""WebVoyager entry point. The orchestration lives in `core.run`; this just wires it.

  python -m benchmarks.webvoyager.run --ids ArXiv--0           # 1-task smoke (our agent)
  python -m benchmarks.webvoyager.run --per-bucket 3           # stratified subset, serial
  python -m benchmarks.webvoyager.run                          # full set (resumable)
  python -m benchmarks.webvoyager.run --runs 3                 # 3 agent re-rolls (mean±std)
  python -m benchmarks.webvoyager.run --system alumnium --concurrency 4
  python -m benchmarks.webvoyager.run --system alumnium --limit 1 --dry-run
"""
from benchmarks.webvoyager.benchmark import build
from core.run import main

if __name__ == "__main__":
    main(build())
