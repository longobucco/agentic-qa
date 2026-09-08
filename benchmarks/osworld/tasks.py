"""Load OSWorld task specs; bucket by app under test."""
import sys

from benchmarks.osworld import config
from core.tasks import load_jsonl

# related_apps naming varies across the dataset for apps we DO support -- different casing,
# spaces instead of underscores, or a shorthand -- and an exact-string scope check treated every
# variant as a missing app. Found live 2026-08-19 alongside the "os" tag fix (see
# config.ALWAYS_PRESENT_CAPABILITIES): together these two normalizations recovered 98 of the 109
# tasks previously marked out of scope. Lowercase + space->underscore handles most variants
# (e.g. "libreoffice calc" -> "libreoffice_calc", "Chrome" -> "chrome") on their own; only
# genuine shorthands need an explicit alias here.
_APP_ALIASES = {"vs_code": "vscode", "calc": "libreoffice_calc", "writer": "libreoffice_writer"}


def _normalize_app(name):
    n = name.strip().lower().replace(" ", "_")
    return _APP_ALIASES.get(n, n)


def bucket_of(task):
    apps = task.get("related_apps") or []
    return apps[0] if apps else "misc"


def app_of(task):
    """The app a task is ABOUT, normalized -- for grouping runs in analysis and reports.

    `bucket_of` deliberately stays raw: it is written into every run record as `bucket` and is
    what the pre-registered G3 strata were drawn on, so changing it would rewrite history
    mid-campaign. This one is re-derived from the task JSON at read time, so it can be correct:
    - normalized, so 'libreoffice calc' / 'vs_code' / 'Chrome' stop splitting one app into
      three rows in every per-app failure breakdown (18 + 6 + 1 tasks in the verified release);
    - capability tags ('os', 'terminal') yield to a real app tag, so ['os', 'chrome'] counts as
      chrome (19 tasks whose FIRST tag is a capability) and only genuinely app-less tasks stay
      under 'os'.
    """
    apps = [_normalize_app(a) for a in (task.get("related_apps") or [])]
    real = [a for a in apps if a not in config.ALWAYS_PRESENT_CAPABILITIES]
    return (real or apps or ["?"])[0]


def load_tasks():
    if not config.TASKS_FILE.exists():
        raise SystemExit(
            f"Missing {config.TASKS_FILE}. Run: "
            f"OSW_RELEASE={config.RELEASE} python -m benchmarks.osworld.data.download_data"
        )
    all_tasks = load_jsonl(config.TASKS_FILE)
    if config.INCLUDE_ALL_APPS:
        return all_tasks
    in_scope = config.SUPPORTED_APPS | config.ALWAYS_PRESENT_CAPABILITIES
    out = [t for t in all_tasks
           if {_normalize_app(a) for a in (t.get("related_apps") or [])} <= in_scope]
    skipped = len(all_tasks) - len(out)
    if skipped:
        # stderr, not stdout: callers that capture load_tasks()'s stdout as data (e.g. the
        # watchdog's --print-order, redirected straight into the driver's task-id list file)
        # would otherwise get this diagnostic line mixed in as a bogus "task id".
        print(f"[osworld] {len(out)} runnable tasks; skipped {skipped} outside the validated "
              f"app scope ({sorted(config.SUPPORTED_APPS)}) — set OSW_INCLUDE_ALL_APPS=1 to run "
              f"everything (expect ENVIRONMENT_ERROR, or a misdiagnosed FAILURE, outside this set)",
              file=sys.stderr)
    return out


def load_refs():
    return {}   # eval spec rides on the task
