"""Re-print the WebVoyager summary for a system. Generic reporting is `core.reporting`.

  python -m benchmarks.webvoyager.report                 # agent_browser (default)
  python -m benchmarks.webvoyager.report alumnium
  python -m benchmarks.webvoyager.report --compare agent_browser alumnium   # harness A/B
"""
import sys

from benchmarks.webvoyager import config
from core.reporting import ab_compare, summarize


def main():
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--compare":
        ab_compare(config.RESULTS_DIR, argv[1], argv[2], title="WebVoyager")
        return
    system = argv[0] if argv else "agent_browser"
    summarize(config.RESULTS_DIR, system, title="WebVoyager")


if __name__ == "__main__":
    main()
