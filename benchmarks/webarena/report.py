"""Re-print the WebArena success-rate summary (deterministic, single run). Generic reporting
is `core.reporting`.

  python -m benchmarks.webarena.report                                     # agent_browser
  python -m benchmarks.webarena.report --compare agent_browser colorbrowser   # vs the SOTA agent
"""
import sys
from benchmarks.webarena import config
from core.reporting import ab_compare, summarize

def main():
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--compare":
        ab_compare(config.RESULTS_DIR, argv[1], argv[2], title="WebArena")
        return
    system = argv[0] if argv else "agent_browser"
    summarize(config.RESULTS_DIR, system, title="WebArena")

if __name__ == "__main__":
    main()
