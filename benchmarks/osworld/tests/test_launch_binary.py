"""Unit tests for env.sandbox._launch_binary's command-line parsing.

Regression coverage for the VLC_VERBOSE=-1 bug: _launch_binary used to take
cmd.split()[0] as "the binary name" unconditionally, so a launch command with a
leading shell-style KEY=VALUE env-var assignment (standard POSIX shell syntax,
used by 18 tasks in the official task set to launch vlc) returned the env
assignment string instead of the actual binary. That silently broke
_verify_launches's window-mapped check (wmctrl -l lists window titles, not
command lines, so it can never match "VLC_VERBOSE=-1").

Pure unit tests: _launch_binary takes a plain dict and returns a string, no
mocks needed.
"""
from benchmarks.osworld.env.sandbox import _launch_binary


def test_leading_env_var_assignment_is_skipped():
    step = {"parameters": {"command": "VLC_VERBOSE=-1 vlc --no-audio --no-video-title-show"}}
    assert _launch_binary(step) == "vlc"


def test_multiple_leading_env_var_assignments_are_skipped():
    step = {"parameters": {"command": "FOO=1 BAR=baz vlc --no-audio"}}
    assert _launch_binary(step) == "vlc"


def test_no_env_var_prefix_common_case_unaffected():
    step = {"parameters": {"command": "google-chrome --no-first-run"}}
    assert _launch_binary(step) == "google-chrome"


def test_list_form_command_unaffected():
    step = {"parameters": {"command": ["google-chrome", "--no-first-run"]}}
    assert _launch_binary(step) == "google-chrome"


def test_path_qualified_binary_after_env_vars():
    step = {"parameters": {"command": "FOO=1 /usr/bin/vlc --no-audio"}}
    assert _launch_binary(step) == "vlc"


def test_empty_command_returns_none():
    step = {"parameters": {"command": ""}}
    assert _launch_binary(step) is None
