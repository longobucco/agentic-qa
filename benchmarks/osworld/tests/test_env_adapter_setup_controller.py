"""The official vscode_config getter calls env.setup_controller._activate_window_setup(...) before
replaying the command palette. Without it every such task ended as EVAL_ERROR (AttributeError on
_EnvAdapter), i.e. was silently dropped from the scored population."""
from benchmarks.osworld import config
from benchmarks.osworld.env import osworld_eval


class _Ctrl:
    base_url = "https://guest.example:443"


def test_adapter_exposes_a_setup_controller_on_the_guest_controller_url():
    env = osworld_eval._EnvAdapter(_Ctrl(), "https://guest.example", [], cache_dir="/tmp")
    sc = env.setup_controller
    assert sc.http_server == "https://guest.example"
    assert (sc.screen_width, sc.screen_height) == (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
    assert hasattr(sc, "_activate_window_setup")


def test_setup_controller_is_built_once_and_lazily():
    env = osworld_eval._EnvAdapter(_Ctrl(), "https://guest.example", [], cache_dir="/tmp")
    assert env._setup_controller is None
    assert env.setup_controller is env.setup_controller
