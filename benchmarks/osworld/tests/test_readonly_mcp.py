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

from benchmarks.osworld.mcp import readonly_server, server

_MUTATING_TOOL_NAMES = (
    "click", "double_click", "right_click", "move", "scroll", "type", "key", "run_python",
)


def _list_tool_names():
    return {t.name for t in asyncio.run(readonly_server.mcp.list_tools())}


def test_list_tools_exposes_exactly_the_readonly_allowlist():
    assert _list_tool_names() == {"screenshot", "a11y_tree", "wait"}


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


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
