"""Unit tests for the pyautogui code-generation in mcp/server.py (no network, no guest):
  python -m benchmarks.osworld.tests.test_mcp_server

Each MCP tool builds a Python source string and ships it to the guest via /run_python.
These tests capture that string (by monkeypatching Controller.pyautogui) and check it's
valid, injection-safe Python -- the class of bug a live run surfaces as an opaque guest-side
SyntaxError, not a clear failure here.
"""
from benchmarks.osworld.mcp import server


def _captured_code(fn, *args):
    calls = []
    orig = server._ctrl.pyautogui
    server._ctrl.pyautogui = lambda code: calls.append(code)
    try:
        fn(*args)
    finally:
        server._ctrl.pyautogui = orig
    return calls[0]


def test_type_handles_multiline_text():
    """Regression: manual quote-escaping missed newlines -- any multi-line type() broke
    with a guest-side SyntaxError. json.dumps must produce valid, compilable source."""
    code = _captured_code(server.type_text, "line1\nline2 with \"quotes\" and \\backslash")
    compile(code, "<test>", "exec")


def test_scroll_honors_both_axes():
    """Regression: dx was silently dropped -- horizontal scroll never worked."""
    code = _captured_code(server.scroll, 5, 10)
    compile(code, "<test>", "exec")
    assert "hscroll(5)" in code
    assert "scroll(10)" in code


def test_scroll_omits_unused_axis():
    code = _captured_code(server.scroll, 0, 7)
    assert "hscroll" not in code
    assert "scroll(7)" in code


def test_key_rejects_injection_safely():
    """Regression: manual quote-joining let a key token break out of the string literal."""
    code = _captured_code(server.key, "ctrl+'; import os; os.system('rm -rf /')  #")
    compile(code, "<test>", "exec")


def test_key_normal_combo():
    code = _captured_code(server.key, "ctrl+s")
    compile(code, "<test>", "exec")
    assert '"ctrl", "s"' in code


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
