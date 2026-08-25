"""OSWorld entry point (setup in README).

  python -m benchmarks.osworld.run --per-bucket 1 --limit 6
"""
from benchmarks.osworld.benchmark import build
from core.run import main

if __name__ == "__main__":
    main(build())
