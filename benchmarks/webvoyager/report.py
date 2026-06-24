"""Re-print the WebVoyager summary for a system. Generic reporting is `core.reporting`.

  python -m benchmarks.webvoyager.report                 # agent_browser (default)
  python -m benchmarks.webvoyager.report alumnium
"""
import sys

from benchmarks.webvoyager import config
from core.reporting import summarize


def main():
    system = sys.argv[1] if len(sys.argv) > 1 else "agent_browser"
    summarize(config.RESULTS_DIR, system, title="WebVoyager")


if __name__ == "__main__":
    main()
