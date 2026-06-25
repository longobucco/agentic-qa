"""BrowseComp entry point. Orchestration lives in `core.run`; this just wires it.

  python -m benchmarks.browsecomp.data.download_data        # once: fetch + decrypt tasks
  python -m benchmarks.browsecomp.run --limit 25 --concurrency 4   # subset smoke
  python -m benchmarks.browsecomp.run --ids browsecomp-0000 --dry-run
  python -m benchmarks.browsecomp.run --per-bucket 3 --runs 2      # stratified by topic
  python -m benchmarks.browsecomp.report                           # re-print accuracy

Needs the Steel agent-browser image: see benchmarks/webvoyager/docker/Dockerfile.steel-ab.
"""
from benchmarks.browsecomp.benchmark import build
from core.run import main

if __name__ == "__main__":
    main(build())
