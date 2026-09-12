"""MCP integrity tests for mcp/readonly_server.py -- the Verify-Replan Auditor's read-only
boundary (docs/verify-replan-minimal-integration-plan.md Sections 3.3, 7.1, 12). No network, no
guest: exercises the FastMCP tool registry directly, not a live controller.

The claim under test is protocol-level, not prompt-level: a mutating tool must be genuinely
ABSENT from tools/list and rejected by call_tool as an unknown tool -- not merely undocumented
or discouraged. Run:
  python -m benchmarks.osworld.tests.test_readonly_mcp

Not covered here (needs a live Daytona controller, deferred to the plan's Live smoke test,
Section 12): that screenshot/a11y_tree calls leave a canary artifact's hash unchanged.
"""
import asyncio
import io
import zipfile

from benchmarks.osworld.mcp import readonly_server, server

_MUTATING_TOOL_NAMES = (
    "click", "double_click", "right_click", "move", "scroll", "type", "key", "run_python",
)


def _list_tool_names():
    return {t.name for t in asyncio.run(readonly_server.mcp.list_tools())}


def test_list_tools_exposes_exactly_the_readonly_allowlist():
    assert _list_tool_names() == {
        "screenshot", "a11y_tree", "wait",
        "inspect_thunderbird_prefs", "inspect_pptx_text_colors",
    }


def test_no_mutating_tool_function_is_even_defined_in_the_module():
    """Stronger than "not registered": the callable itself must not exist in this module's
    namespace, so there is no code path that could register or invoke it later by accident."""
    for name in ("click", "double_click", "right_click", "move", "scroll", "type_text", "key",
                 "run_python"):
        assert not hasattr(readonly_server, name), f"{name} must not be defined in readonly_server"


def test_calling_a_mutating_tool_name_fails_at_the_protocol_level():
    """A prompt telling the model "don't act" is not the boundary under test here -- this
    confirms the MCP protocol itself has nothing to invoke, the same way --strict-mcp-config
    was verified live (2026-08-30) to produce an honest "not found" rather than a confabulated
    substitute for a genuinely-absent tool."""
    for name in _MUTATING_TOOL_NAMES:
        try:
            asyncio.run(readonly_server.mcp.call_tool(name, {"x": 1, "y": 1}))
            raised = False
        except Exception:
            raised = True
        assert raised, f"call_tool({name!r}) should have failed -- tool must not exist"


def test_readonly_server_shares_the_full_servers_screenshot_and_a11y_implementation():
    """The two servers must observe identically -- an Auditor whose screenshot() differs even
    slightly from the acting agent's own view would be judging a different desktop, not the
    same one from a restricted angle. Compared by source, since both close over their own
    module-level `_ctrl` instance (same Controller class, same OSW_CONTROLLER_URL contract) and
    are therefore never `is`-identical objects."""
    import inspect
    assert (inspect.getsource(readonly_server.screenshot).split("\n", 1)[1]
            == inspect.getsource(server.screenshot).split("\n", 1)[1])
    assert (inspect.getsource(readonly_server.a11y_tree).split("\n", 1)[1]
            == inspect.getsource(server.a11y_tree).split("\n", 1)[1])
    assert (inspect.getsource(readonly_server.wait).split("\n", 1)[1]
            == inspect.getsource(server.wait).split("\n", 1)[1])


def _patched_ctrl(execute=None, read_file=None):
    real_execute, real_read_file = readonly_server._ctrl.execute, readonly_server._ctrl.read_file
    if execute is not None:
        readonly_server._ctrl.execute = execute
    if read_file is not None:
        readonly_server._ctrl.read_file = read_file
    return real_execute, real_read_file


def _restore_ctrl(real_execute, real_read_file):
    readonly_server._ctrl.execute = real_execute
    readonly_server._ctrl.read_file = real_read_file


def test_inspect_thunderbird_prefs_returns_only_matching_lines():
    prefs_text = (
        'user_pref("mail.server.default.applyIncomingFilters", true);\n'
        'user_pref("mail.identity.id1.fullName", "Test User");\n'
        'user_pref("mail.server.server1.applyIncomingFilters", false);\n'
    )
    real = _patched_ctrl(
        execute=lambda cmd, **k: "/home/user/.thunderbird/abc.default/prefs.js",
        read_file=lambda path: prefs_text.encode(),
    )
    try:
        out = readonly_server.inspect_thunderbird_prefs("applyIncomingFilters")
    finally:
        _restore_ctrl(*real)
    lines = out.splitlines()
    assert len(lines) == 2
    assert all("applyIncomingFilters" in l for l in lines)
    assert not any("fullName" in l for l in lines)


def test_inspect_thunderbird_prefs_case_insensitive_plain_substring_not_a_shell_pattern():
    """The pattern is a model-supplied string -- it must only ever be compared as plain text,
    never interpolated into a shell command or regex that could behave unexpectedly on
    metacharacters."""
    prefs_text = 'user_pref("mail.dark-reader.enabled", true);\n'
    real = _patched_ctrl(
        execute=lambda cmd, **k: "/x/prefs.js", read_file=lambda p: prefs_text.encode())
    try:
        out = readonly_server.inspect_thunderbird_prefs("DARK-READER")
        out_meta = readonly_server.inspect_thunderbird_prefs("dark.reader")   # regex metachar '.'
    finally:
        _restore_ctrl(*real)
    assert "dark-reader" in out
    assert "no user_pref line" in out_meta   # literal '.' must NOT match the literal '-'


def test_inspect_thunderbird_prefs_reports_when_no_profile_found():
    real = _patched_ctrl(execute=lambda cmd, **k: "")
    try:
        out = readonly_server.inspect_thunderbird_prefs("anything")
    finally:
        _restore_ctrl(*real)
    assert "no prefs.js found" in out


def _fake_pptx_bytes(slides_xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, xml in enumerate(slides_xml, start=1):
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


def test_inspect_pptx_text_colors_extracts_hex_and_text_per_run():
    xml = (
        '<a:r><a:rPr><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></a:rPr>'
        '<a:t>LAUNCH</a:t></a:r>'
    )
    real = _patched_ctrl(read_file=lambda p: _fake_pptx_bytes([xml]))
    try:
        out = readonly_server.inspect_pptx_text_colors("/x/45_2.pptx")
    finally:
        _restore_ctrl(*real)
    assert "#FF0000" in out and "LAUNCH" in out


def test_inspect_pptx_text_colors_reports_a_bad_zip_without_crashing():
    real = _patched_ctrl(read_file=lambda p: b"not a zip file at all")
    try:
        out = readonly_server.inspect_pptx_text_colors("/x/broken.pptx")
    finally:
        _restore_ctrl(*real)
    assert "not a valid .pptx" in out


def test_inspect_pptx_text_colors_reports_a_read_failure_without_crashing():
    def boom(path):
        raise RuntimeError("guest unreachable")
    real = _patched_ctrl(read_file=boom)
    try:
        out = readonly_server.inspect_pptx_text_colors("/x/y.pptx")
    finally:
        _restore_ctrl(*real)
    assert "could not read" in out


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
