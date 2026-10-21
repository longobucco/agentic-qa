"""Pure regression tests for env.sandbox's _verify_launches -- no live sandbox. Covers the
window-mapped check added on top of the pre-existing process-alive (pgrep) check: under a cold
container start, a process can be alive for seconds before painting anything, and the agent's
first screenshot can be handed over blank (observed: closed-book tasks
215dfd39/a5bbbcd5/28cc3b7e run 1). A process-only check would have missed this."""
from unittest.mock import MagicMock, patch

from benchmarks.osworld.env import sandbox

STEPS = [{"type": "launch", "parameters": {"command": "vlc --no-audio"}}]


def _fake_ctrl(side_effect):
    ctrl = MagicMock()
    ctrl.execute.side_effect = side_effect
    return ctrl


def test_process_running_and_window_mapped_succeeds():
    # wmctrl output deliberately uppercased ("VLC-Window-Title") against a lowercase binary
    # name ("vlc") so this test actually exercises the `.lower()` calls in _window_mapped --
    # it must fail if either `.lower()` were removed from the implementation.
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return "0x00000001  0 VLC-Window-Title"
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is None


def test_process_running_but_window_never_mapped_fails():
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return "0x00000001  0 some-other-app"
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is not None
    assert "never started/rendered" in result
    assert "vlc" in result


def test_wmctrl_error_falls_back_to_true_and_does_not_block_success():
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return None
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    assert sandbox._window_mapped(ctrl, "vlc") is True
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, STEPS)
    assert result is None


SOCAT_STEPS = [{"type": "launch",
                "parameters": {"command": "socat tcp-listen:9222,fork tcp:localhost:1337"}}]


def test_headless_binary_with_no_window_still_succeeds():
    # socat is a headless CDP-forwarding TCP relay (used alongside google-chrome in 79 real
    # OSWorld task configs) -- it never maps a window. If _window_mapped were ever actually
    # invoked for it, wmctrl is rigged to return a string that does NOT contain "socat", which
    # would fail the old (pre-exemption) check. The exemption must short-circuit before that.
    def side_effect(command, *, shell=False, timeout=120):
        if "pgrep" in command:
            return "12345"
        if "wmctrl" in command:
            return "0x00000001  0 some-other-app"
        raise AssertionError(f"unexpected command: {command}")

    ctrl = _fake_ctrl(side_effect)
    with patch("time.sleep"):
        result = sandbox._verify_launches(ctrl, SOCAT_STEPS)
    assert result is None


def test_window_mapped_short_circuits_for_headless_binary_without_calling_wmctrl():
    # Proves the headless exemption happens BEFORE any wmctrl call, not just that it happens to
    # pass despite one -- ctrl.execute is rigged to raise if invoked at all.
    ctrl = MagicMock()
    ctrl.execute.side_effect = AssertionError("wmctrl should never be called for socat")
    assert sandbox._window_mapped(ctrl, "socat") is True
    ctrl.execute.assert_not_called()


def test_window_mapped_title_only_output_still_fails_without_wm_class():
    # Isolates what -x specifically buys us: a window whose TITLE never mentions the app name at
    # all (e.g. a document/page title, the common real-world case), so neither the raw substring
    # check NOR the hyphen/underscore normalization can bridge title text to binary name -- only
    # a WM_CLASS column could. NOTE: an earlier draft of this test used a title that spelled out
    # "Google Chrome" (space-separated) with no class column, expecting False. That was wrong:
    # under the exact fix code, normalize(name) = "google chrome" is already a literal substring
    # of any title that spells the app name with a space (e.g. "New Tab - Google Chrome"), so the
    # normalization step alone (independent of -x) resolves that particular case. This test uses
    # a title that names neither the binary nor its human-readable form, which is what actually
    # requires WM_CLASS.
    def side_effect(command, *, shell=False, timeout=120):
        return "0x00000001  0 Untitled Document - MyEditor"

    ctrl = _fake_ctrl(side_effect)
    assert sandbox._window_mapped(ctrl, "google-chrome") is False


def test_window_mapped_matches_via_wm_class_column():
    # The actual fix validation: a realistic `wmctrl -lx` line carries a WM_CLASS column
    # ("google-chrome.Google-chrome") that echoes the binary name verbatim, even though the
    # TITLE column ("New Tab - Google Chrome") still would not match on its own.
    def side_effect(command, *, shell=False, timeout=120):
        return ("0x00000001  0 google-chrome.Google-chrome  host  "
                "New Tab - Google Chrome")

    ctrl = _fake_ctrl(side_effect)
    assert sandbox._window_mapped(ctrl, "google-chrome") is True


def test_window_mapped_uses_wmctrl_lx_command():
    # Confirms the actual command sent is "wmctrl -lx" (the -x flag), not the old "wmctrl -l".
    ctrl = MagicMock()
    ctrl.execute.return_value = "0x00000001  0 google-chrome.Google-chrome  host  New Tab"
    sandbox._window_mapped(ctrl, "google-chrome")
    ctrl.execute.assert_called_once()
    called_command = ctrl.execute.call_args.args[0]
    assert called_command == "wmctrl -lx"


def test_window_mapped_normalizes_hyphen_underscore_mismatch():
    # Generic normalization fallback: binary name "my_app" (underscore) vs. WM_CLASS
    # "my-app.MyApp" (hyphen) -- the raw substring check fails on punctuation alone; only the
    # hyphen/underscore-to-space normalization makes them equal ("my app" == "my app").
    def side_effect(command, *, shell=False, timeout=120):
        return "0x00000001  0 my-app.MyApp  host  Some Window Title"

    ctrl = _fake_ctrl(side_effect)
    # Sanity: prove the raw (non-normalized) substring check alone would fail, so this test is
    # actually exercising the normalization fallback and not a lucky substring match.
    raw_out = side_effect("wmctrl -lx")
    assert "my_app" not in raw_out.lower()
    assert sandbox._window_mapped(ctrl, "my_app") is True
