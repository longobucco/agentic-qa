"""OSWorld read-only MCP server: the Verify-Replan Auditor's ONLY view of the desktop
(docs/verify-replan-minimal-integration-plan.md Section 3.3, 7.1). A prompt telling the model
"don't act" is not a security boundary -- server.py's own `run_python` gate already establishes
that precedent for OSWorld (registered only when OSW_RESTRICT_RUN_PYTHON isn't set). This module
takes that one step further: it never imports or defines a single mutating tool function, so
there is nothing for the protocol's own `tools/list` to expose and nothing for a model to invoke
even if it tried -- not a filtered view of server.py, a module that structurally cannot mutate.

Exposes exactly: `screenshot`, `a11y_tree`, `wait`. `wait` is included because it only sleeps
(see `benchmarks.osworld.mcp.server.wait`) -- verified non-mutating by test_readonly_mcp.py's
canary-hash check, not by inspection alone.

    OSW_CONTROLLER_URL=http://host:5000 python -m benchmarks.osworld.mcp.readonly_server
"""
import os
import time

from mcp.server.fastmcp import FastMCP, Image

from benchmarks.osworld.env.controller import Controller

mcp = FastMCP("osworld_readonly")
_ctrl = Controller(os.environ.get("OSW_CONTROLLER_URL", "http://localhost:5000"))


@mcp.tool()
def screenshot() -> Image:
    """PNG of the current screen."""
    return Image(data=_ctrl.screenshot(), format="png")


@mcp.tool()
def a11y_tree() -> str:
    """Accessibility tree, for grounding."""
    return _ctrl.a11y_tree()


@mcp.tool()
def wait(seconds: float) -> str:
    time.sleep(min(seconds, 30))
    return f"waited {seconds}s"


if __name__ == "__main__":
    mcp.run()
