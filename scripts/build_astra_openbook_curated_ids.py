"""Curate the open-book population for a first real campaign: the full 308-task classifier
output (scripts/g_astra_openbook_ids.txt) minus two categories the runner cannot complete
end-to-end today, regardless of fixture curation or the CdpForwarder fix:

  - 8 'external_setup:login' tasks (SetupController._login_setup) -- see
    astra_openbook_campaign_lock.json's known_issues.login_setup_not_supported. 7 of these 8
    ALSO have a 'download' config step and so already have real fixture content authored
    (download_population_fixtures_curated) -- that content is real and correct, but the task
    still can't complete because its login step has no fixture-served counterpart at all.
  - 4 tasks whose evaluator (check_url_and_content_include) needs real interactive commerce-
    site content (Delta/Kohl's/NFL.com/Macy's-style body-text/URL checks) that was deliberately
    not attempted -- see known_issues.remaining_population_surveyed_2026_09_13.

    python -m scripts.build_astra_openbook_curated_ids > scripts/g_astra_openbook_curated_ids.txt
"""
import sys

from benchmarks.osworld import closed_book
from benchmarks.osworld.tasks import load_tasks

_HARD_ECOMMERCE_IDS = {
    "7f52cab9-535c-4835-ac8c-391ee64dc930",
    "9f3f70fc-5afc-4958-a7b7-3bb4fcb01805",
    "cabb3bae-cccb-41bd-9f5d-0f3a9fecd825",
    "f0b971a1-6831-4b9b-a50e-22a6e47f45ba",
}


def curated_ids():
    ids = []
    for task in load_tasks():
        classification = closed_book.classify(task)
        if classification["book"] != "open-book":
            continue
        task_id = classification["task_id"]
        if task_id in _HARD_ECOMMERCE_IDS:
            continue
        if any("login" in reason for reason in classification["reasons"]):
            continue
        ids.append(task_id)
    return sorted(ids)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ids = curated_ids()
    print("# Curated from scripts/g_astra_openbook_ids.txt by "
         "scripts/build_astra_openbook_curated_ids.py:")
    print(f"# the full {len(ids) + 12} minus 8 'external_setup:login' tasks and 4 tasks needing "
         f"real interactive commerce-site content (check_url_and_content_include).")
    for task_id in ids:
        print(task_id)


if __name__ == "__main__":
    main()
