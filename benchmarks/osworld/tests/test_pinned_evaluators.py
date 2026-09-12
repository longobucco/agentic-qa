"""Unit tests for the pinned-evaluator overlay (offline, no sandbox):
  python -m benchmarks.osworld.tests.test_pinned_evaluators

These exist because the first version of the overlay was a silent no-op: `__path__` only steers
imports that haven't happened yet, and importing `desktop_env.controllers.setup` -- which every
run does, for config setup -- had already pulled the installed metrics into sys.modules. 171
runs recorded `evaluator_commit` while the installed release computed their verdicts. Each test
below pins one link of that chain.
"""
import inspect
import pathlib
import sys

from benchmarks.osworld.env import osworld_eval

PINNED = osworld_eval.PINNED_EVALUATORS


def _pinned_tree_present():
    return (PINNED / "PINNED_COMMIT").is_file()


def test_the_overlay_wins_even_when_setup_imported_the_metrics_first():
    """The exact ordering of a real run: config setup, then scoring."""
    if not _pinned_tree_present():
        print("   (skipped: run python -m benchmarks.osworld.data.download_evaluators)")
        return
    import desktop_env.controllers.setup            # noqa: F401  (drags in installed metrics)
    osworld_eval.use_pinned_evaluators()
    from desktop_env.evaluators import metrics, getters
    for mod in (metrics, getters):
        assert str(pathlib.Path(inspect.getfile(mod)).parent).startswith(str(PINNED)), \
            f"{mod.__name__} still resolving to {inspect.getfile(mod)}"


def test_the_pinned_tree_has_the_symbols_the_installed_release_lacks():
    if not _pinned_tree_present():
        return
    osworld_eval.use_pinned_evaluators()
    from desktop_env.evaluators import metrics, getters
    for sym in ("compare_references_gain", "check_play_and_exit", "compare_pptx_files_tolerant",
                "compare_pptx_files_robust", "check_continuation_line_indent_no_bullet"):
        assert hasattr(metrics, sym), sym
    for sym in ("get_local_file", "get_chrome_appearance_mode_ui"):
        assert hasattr(getters, sym), sym


def test_provenance_reports_where_the_metrics_really_came_from():
    """A provenance field that lies is worse than no field: this must never say "pinned"
    unless the pinned tree is what actually answered."""
    if not _pinned_tree_present():
        return
    osworld_eval.use_pinned_evaluators()
    prov = osworld_eval.evaluator_provenance()
    assert prov["evaluator_source"] == "pinned"
    assert prov["evaluator_commit"]
    from desktop_env.evaluators import metrics
    assert str(pathlib.Path(inspect.getfile(metrics)).parent).startswith(str(PINNED))


def test_provenance_says_installed_when_the_overlay_is_switched_off():
    from benchmarks.osworld import config
    was = config.PINNED_EVALUATORS
    config.PINNED_EVALUATORS = False
    try:
        # force a fresh resolution so the assertion is about this call, not a cached module
        for name in [n for n in sys.modules if n.startswith("desktop_env.evaluators.")]:
            del sys.modules[name]
        prov = osworld_eval.evaluator_provenance()
        assert prov["evaluator_commit"] is None
        assert prov["evaluator_source"] == "installed"
    finally:
        config.PINNED_EVALUATORS = was
        osworld_eval.use_pinned_evaluators()


def test_the_env_adapter_carries_the_attributes_the_pinned_getters_read():
    """vm_machine joined chromium_port/vlc_port on this list the day the pinned tree went in:
    getters/chrome.py reads env.vm_machine.lower() and cost 3 runs on 873cafdd."""
    class _Ctrl:
        def execute_python_command(self, _code):
            return {"output": "x86_64\n"}

    env = osworld_eval._EnvAdapter(_Ctrl(), "https://host", [], cache_dir="/tmp")
    for attr in ("vm_ip", "server_port", "vm_platform", "vm_machine", "chromium_port",
                 "vlc_port", "current_use_proxy", "cache_dir", "controller"):
        assert hasattr(env, attr), attr
    assert env.vm_machine == "x86_64"


def test_vm_machine_falls_back_instead_of_failing_the_whole_evaluation():
    class _Dead:
        def execute_python_command(self, _code):
            raise ConnectionError("guest is gone")

    assert osworld_eval._guest_machine(_Dead()) == "x86_64"


if __name__ == "__main__":
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
        print("ok", name)
