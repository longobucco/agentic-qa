"""Curate the open-book population for a first real campaign: the full 308-task classifier
output (scripts/g_astra_openbook_ids.txt) minus categories the runner cannot complete
end-to-end today, regardless of fixture curation or the CdpForwarder fix:

  - 8 'external_setup:login' tasks (SetupController._login_setup) -- see
    astra_openbook_campaign_lock.json's known_issues.login_setup_not_supported. 7 of these 8
    ALSO have a 'download' config step and so already have real fixture content authored
    (download_population_fixtures_curated) -- that content is real and correct, but the task
    still can't complete because its login step has no fixture-served counterpart at all.
  - 4 tasks whose evaluator (check_url_and_content_include) needs real interactive commerce-
    site content (Delta/Kohl's/NFL.com/Macy's-style body-text/URL checks) that was deliberately
    not attempted -- see known_issues.remaining_population_surveyed_2026_09_13.
  - 'chrome_open_tabs'/'chrome_close_tabs' tasks whose evaluator expects a URL/tab/bookmark
    state DIFFERENT from what the config step already opened -- confirmed live 2026-09-13/14
    (known_issues.chrome_open_tabs_deep_navigation_needs_content) that these need the agent to
    navigate FURTHER into a real site (find a specific product/form/page) to reach the expected
    state, impossible against a fixture_miss page regardless of the CDP routing fix. A task is
    kept only when EVERY expected URL/bookmark the evaluator names is a substring (either way)
    of one of the URLs chrome_open_tabs already opened -- i.e. no further navigation is implied.
    This rule was checked against all 20 live-tested chrome_open_tabs tasks from the 2026-09-13
    batch canaries and matches the outcome exactly: it keeps exactly the 2 that scored SUCCESS
    (06fe7178, 7a5a7856) and excludes all 13 that scored FAILURE -- not a guess, a confirmed
    predictor over every live data point collected so far.

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

_URL_STATE_FUNCS = {"is_expected_active_tab", "is_expected_active_tab_approximate",
                    "is_expected_url_pattern_match", "is_expected_tabs", "is_expected_bookmarks"}


def _specs(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _opened_urls(task):
    urls = []
    for step in task.get("config") or []:
        if str(step.get("type", "")).lower() == "chrome_open_tabs":
            urls.extend(step.get("parameters", {}).get("urls_to_open", []))
    return urls


def _has_chrome_open_tabs(task):
    return any(str(s.get("type", "")).lower() == "chrome_open_tabs" for s in (task.get("config") or []))


def _expected_urls(ev):
    """(urls, unknown_shape) -- unknown_shape True means some expected spec's shape isn't one
    we know how to compare against opened URLs, so the caller must NOT treat the task as safe."""
    out = []
    unknown = False
    for exp in _specs(ev.get("expected")):
        if not isinstance(exp, dict):
            unknown = True
            continue
        rules = exp.get("rules") or {}
        if not isinstance(rules, dict):
            unknown = True
            continue
        rtype = rules.get("type")
        if rtype == "url":
            u = rules.get("url")
            if u:
                out.append(u)
            out.extend(rules.get("urls") or [])
        elif rtype == "bookmark_bar_websites_urls":
            out.extend(rules.get("urls") or [])
        elif isinstance(rules.get("expected"), list):
            out.extend(rules["expected"])
        elif isinstance(rules.get("expected"), str):
            out.append(rules["expected"])
        else:
            unknown = True
    return out, unknown


def _chrome_open_tabs_needs_deep_navigation(task):
    if not _has_chrome_open_tabs(task):
        return False
    ev = task.get("evaluator") or {}
    func = ev.get("func")
    funcs = set(func if isinstance(func, list) else [func])
    if not (funcs <= _URL_STATE_FUNCS):
        return True   # any non-URL-state evaluator (is_added_to_steam_cart, exact_match, ...)
    opened = _opened_urls(task)
    exp_urls, unknown = _expected_urls(ev)
    if unknown or not exp_urls:
        return True
    return not all(any(eu in o or o in eu for o in opened) for eu in exp_urls)


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
        if _chrome_open_tabs_needs_deep_navigation(task):
            continue
        ids.append(task_id)
    return sorted(ids)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ids = curated_ids()
    print("# Curated from scripts/g_astra_openbook_ids.txt by "
         "scripts/build_astra_openbook_curated_ids.py:")
    print("# the full 308 minus 8 'external_setup:login' tasks, 4 tasks needing real "
         "interactive commerce-site content (check_url_and_content_include), and "
         "chrome_open_tabs tasks needing deep navigation into a real site (see this script's "
         "own docstring for the exact, live-confirmed rule).")
    for task_id in ids:
        print(task_id)


if __name__ == "__main__":
    main()
