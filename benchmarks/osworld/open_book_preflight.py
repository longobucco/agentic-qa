"""Campaign-wide and per-task preflight for the open-book GPT Astra runner
(runners/gpt_astra_openbook.py). Mirrors gpt_astra.py's own _validate_campaign_lock/preflight
pattern against astra_openbook_campaign_lock.json instead of the closed-book lock, plus the
task-scoped fixture checks the closed-book runner has no equivalent of (see
docs/g_astra_open_book_runner_implementation.md's "Preflight contract").
"""
import hashlib
import json
from pathlib import Path

from benchmarks.osworld import closed_book, config
from benchmarks.osworld.runners import astra_common
from core.codex_loop import ALLOWED_MCP_TOOLS, APPROVAL_MODE, DISABLED_FEATURES

_ROOT = Path(__file__).resolve().parents[2]
_LOCK = Path(__file__).resolve().parent / "astra_openbook_campaign_lock.json"
_FIXTURES_DIR = Path(__file__).resolve().parent / "openbook_fixtures"


class PreflightError(RuntimeError):
    pass


def load_lock():
    return json.loads(_LOCK.read_text())


def _read_ids(rel_path):
    return [line for line in (_ROOT / rel_path).read_text().splitlines()
           if line.strip() and not line.lstrip().startswith("#")]


def _validate_lock(lock):
    """Same discipline as gpt_astra._validate_campaign_lock, against this campaign's own lock
    shape. Any mismatch fails before a desktop is provisioned (the spec's own requirement) --
    including, deliberately, the draft status itself: a lock not yet marked "frozen" (no
    authored fixtures, no passed canary) must refuse to run anything for real."""
    if lock.get("status") != "frozen":
        raise SystemExit(
            f"astra_openbook_campaign_lock.json status={lock.get('status')!r}, not 'frozen' -- "
            f"refusing to run against a draft lock (see its own _status_note)"
        )
    if lock["release"] != "osworld_verified" or config.RELEASE != "verified":
        raise SystemExit(
            f"Astra open-book release differs from frozen OSWorld Verified: {config.RELEASE!r}")
    expected = {
        "model": config.ASTRA_MODEL,
        "reasoning_effort": config.ASTRA_REASONING_EFFORT,
        "codex_cli_version": config.ASTRA_CODEX_VERSION,
    }
    for key, actual in expected.items():
        if lock[key] != actual:
            raise SystemExit(
                f"Astra open-book campaign lock mismatch for {key}: {actual!r} != {lock[key]!r}")
    population = lock["population"]
    ids = [task_id for path in population["paths"] for task_id in _read_ids(path)]
    digest = hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()
    if digest != population["sha256"]:
        raise SystemExit(f"Astra open-book population hash mismatch: {digest}")
    if len(ids) != population["count"] or len(set(ids)) != len(ids):
        raise SystemExit(
            "Astra open-book population count/uniqueness differs from the frozen campaign lock")
    policy = lock["tool_policy"]
    if (policy["approval_mode"] != APPROVAL_MODE
            or tuple(policy["disabled_features"]) != DISABLED_FEATURES
            or tuple(policy["allowed_mcp_tools"]) != ALLOWED_MCP_TOOLS):
        raise SystemExit("Astra open-book tool policy differs from the frozen campaign lock")


def campaign_check():
    """Steps 1-5 of the spec's "Preflight contract": lock/population/policy validation plus the
    same Codex CLI/model/effort catalog check the closed-book runner already does (shared via
    astra_common.check_codex_cli). Daytona image digest / guest Chrome-profile / MCP-version
    checks are deferred to the live canary (see the plan's "Explicitly deferred" note) -- they
    need a real provisioned sandbox to probe, which this campaign-wide, no-sandbox check does
    not have."""
    lock = load_lock()
    _validate_lock(lock)
    astra_common.check_codex_cli(
        model=config.ASTRA_MODEL, reasoning_effort=config.ASTRA_REASONING_EFFORT,
        codex_cli_version=config.ASTRA_CODEX_VERSION,
    )
    return lock


def _population_ids(lock):
    population = lock["population"]
    return {task_id for path in population["paths"] for task_id in _read_ids(path)}


def require_open_book(task, *, lock=None):
    """Raises PreflightError if `task` is not in the frozen population, or no longer classifies
    as open-book (the population was frozen against a snapshot of closed_book.classify(); a
    changed task set or classifier would otherwise run a closed-book task through the open-book
    proxy/lockdown machinery unnoticed)."""
    lock = lock or load_lock()
    if task["id"] not in _population_ids(lock):
        raise PreflightError(
            f"task {task['id']} is not in the frozen open-book population")
    classification = closed_book.classify(task)
    if classification["book"] != "open-book":
        raise PreflightError(
            f"task {task['id']} now classifies as {classification['book']!r}, not open-book -- "
            f"refusing (population may be stale relative to the current task data/classifier)")


def task_bundle_dir(task):
    return _FIXTURES_DIR / task["id"]


def task_check(task, *, lock=None):
    """Task-scoped preflight (spec steps 1-6). Never raises for an ordinary missing/broken
    fixture -- that is ENVIRONMENT_ERROR territory the caller (runner) writes to result.json,
    not a crash. Returns {"ready": bool, "reason": str|None, "bundle_dir": str|None,
    "manifest_sha256": str|None}."""
    lock = lock or load_lock()
    result = {"ready": False, "reason": None, "bundle_dir": None, "manifest_sha256": None}
    try:
        require_open_book(task, lock=lock)
    except PreflightError as e:
        result["reason"] = f"NOT_OPEN_BOOK: {e}"
        return result
    bundle_dir = task_bundle_dir(task)
    result["bundle_dir"] = str(bundle_dir)
    if not (bundle_dir / "manifest.json").is_file():
        result["reason"] = f"SNAPSHOT_PROXY_MISSING: no fixture bundle at {bundle_dir}"
        return result
    try:
        from benchmarks.osworld.openbook_proxy.bundle import FixtureBundle
        bundle = FixtureBundle.load(bundle_dir)
    except Exception as e:
        result["reason"] = f"SNAPSHOT_PROXY_MISSING: fixture bundle failed to load: {e}"
        return result
    result.update(ready=True, manifest_sha256=bundle.sha256())
    return result


def write_preflight_json(out, result):
    (out / "preflight.json").write_text(json.dumps(result, indent=2))
    return result
