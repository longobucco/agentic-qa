"""Load OSWorld task specs; bucket by app under test."""
from benchmarks.osworld import config
from core.tasks import load_jsonl

# related_apps naming varies across the dataset for apps we DO support -- different casing,
# spaces instead of underscores, or a shorthand -- and an exact-string scope check treated every
# variant as a missing app. Found live 2026-08-19 alongside the "os" tag fix (see
# config.ALWAYS_PRESENT_CAPABILITIES): together these two normalizations recovered 98 of the 109
# tasks previously marked out of scope. Lowercase + space->underscore handles most variants
# (e.g. "libreoffice calc" -> "libreoffice_calc", "Chrome" -> "chrome") on their own; only
# genuine shorthands need an explicit alias here.
_APP_ALIASES = {"vs_code": "vscode", "calc": "libreoffice_calc", "writer": "libreoffice_writer",
                "browser": "chrome"}

_LOGIN_STEP_TYPES = {"login", "googledrive"}


def is_login_task(task):
    """Needs a real account login during setup -- excluded from the published 361-task
    OSWorld-Verified population (369 - 8 = 361)."""
    return any(s.get("type") in _LOGIN_STEP_TYPES for s in (task.get("config") or []))


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
    return [t for t in all_tasks if not is_login_task(t)]


def load_refs():
    return {}   # eval spec rides on the task
