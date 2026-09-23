"""Config and postconfig steps must run on the SetupController of the same upstream commit as the
task data (download_data.UPSTREAM_COMMIT). The installed desktop_env 1.0.2 is older: 2 tasks use a
postconfig step type it lacks (chrome_inject_js) and 1 passes a launch parameter it rejects
(wait_for_cdp) -- their setup/postconfig died regardless of the agent."""
import inspect
import json

import pytest

from benchmarks.osworld import config
from benchmarks.osworld.env import osworld_eval
from benchmarks.osworld.tasks import is_login_task
from core.tasks import load_jsonl

pinned = pytest.mark.skipif(
    not (osworld_eval.PINNED_CONTROLLERS / "PINNED_COMMIT").is_file(),
    reason="pinned controllers not downloaded (python -m benchmarks.osworld.data.download_evaluators)")


def _unsupported_steps(sc_class):
    bad = []
    for t in load_jsonl(config.TASKS_FILE):
        if is_login_task(t):
            continue
        for steps in (t.get("config") or [], t["evaluator"].get("postconfig") or []):
            for s in steps:
                fn = getattr(sc_class, f"_{s['type']}_setup", None)
                if fn is None:
                    bad.append((t["id"], s["type"], "missing step type"))
                    continue
                params = inspect.signature(fn).parameters
                if any(p.kind is p.VAR_KEYWORD for p in params.values()):
                    continue   # SetupController.setup() calls fn(**parameters): **kwargs takes all
                bad += [(t["id"], s["type"], k) for k in (s.get("parameters") or {})
                        if k not in params]
    return bad


@pinned
def test_setup_controller_in_use_is_the_pinned_one():
    sc = osworld_eval.make_setup_controller("http://127.0.0.1:5000")
    assert type(sc).__module__ == "desktop_env.controllers.setup"
    assert inspect.getsourcefile(type(sc)).startswith(str(osworld_eval.PINNED_CONTROLLERS))


@pinned
def test_every_step_of_the_361_is_supported_by_the_setup_controller_in_use():
    sc = osworld_eval.make_setup_controller("http://127.0.0.1:5000")
    assert _unsupported_steps(type(sc)) == []


@pinned
def test_provenance_records_the_setup_controller_commit():
    prov = osworld_eval.evaluator_provenance()
    assert prov["setup_controller_commit"] == (
        osworld_eval.PINNED_CONTROLLERS / "PINNED_COMMIT").read_text().strip()


def test_the_check_catches_the_installed_release_gaps():
    """The same check against the installed 1.0.2 finds the gaps this pin exists to close --
    proof the check above can fail."""
    import importlib.util
    import desktop_env
    path = __import__("pathlib").Path(desktop_env.__file__).parent / "controllers" / "setup.py"
    spec = importlib.util.spec_from_file_location("_installed_setup_1_0_2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    kinds = {(step, what) for _, step, what in _unsupported_steps(mod.SetupController)}
    assert ("chrome_inject_js", "missing step type") in kinds
