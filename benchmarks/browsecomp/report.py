"""Re-print the BrowseComp accuracy summary. Generic reporting is `core.reporting`.

  python -m benchmarks.browsecomp.report                          # agent_browser (default)
  python -m benchmarks.browsecomp.report claude_code              # the A/B comparator
  python -m benchmarks.browsecomp.report --compare agent_browser claude_code   # harness A/B

Accuracy = pass rate from the answer-vs-reference grader, per topic bucket. (Calibration —
binning the captured `confidence` against correctness — is a planned follow-up.)
"""
import sys

from benchmarks.browsecomp import config
from core.reporting import ab_compare, summarize


def main():
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--compare":
        ab_compare(config.RESULTS_DIR, argv[1], argv[2], title="BrowseComp")
        return
    system = argv[0] if argv else "agent_browser"
    summarize(config.RESULTS_DIR, system, title="BrowseComp")


if __name__ == "__main__":
    main()
