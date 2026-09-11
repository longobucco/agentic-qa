"""OSWorld MCP server: desktop tools proxied to the in-guest controller. The runner launches it
per task with OSW_CONTROLLER_URL set. Needs the `mcp` package.

    OSW_CONTROLLER_URL=http://host:5000 python -m benchmarks.osworld.mcp.server
"""
import json
import os
import time

from mcp.server.fastmcp import FastMCP, Image

from benchmarks.osworld.env.controller import Controller

mcp = FastMCP("osworld")
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
def click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.click({x}, {y})")
    return f"clicked ({x}, {y})"


@mcp.tool()
def double_click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.doubleClick({x}, {y})")
    return f"double-clicked ({x}, {y})"


@mcp.tool()
def right_click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.rightClick({x}, {y})")
    return f"right-clicked ({x}, {y})"


@mcp.tool()
def move(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.moveTo({x}, {y})")
    return f"moved to ({x}, {y})"


@mcp.tool()
def scroll(dx: int, dy: int) -> str:
    code = "import pyautogui; "
    if dy:
        code += f"pyautogui.scroll({dy}); "
    if dx:
        code += f"pyautogui.hscroll({dx})"
    _ctrl.pyautogui(code)
    return f"scrolled dx={dx} dy={dy}"


@mcp.tool(name="type")
def type_text(text: str) -> str:
    # json.dumps produces a fully-escaped double-quoted literal (newlines, quotes,
    # backslashes, unicode) -- a manual .replace() missed newlines, which broke any
    # multi-line text as a syntax error in the generated source (see the guest's /run_python)
    _ctrl.pyautogui(f"import pyautogui; pyautogui.write({json.dumps(text)}, interval=0.02)")
    return f"typed {len(text)} chars"


@mcp.tool()
def key(keys: str) -> str:
    """Press a combo, e.g. 'ctrl+s'."""
    tokens = json.dumps([k.strip() for k in keys.split("+")])
    _ctrl.pyautogui(f"import pyautogui; pyautogui.hotkey(*{tokens})")
    return f"pressed {keys}"


if os.environ.get("OSW_RESTRICT_RUN_PYTHON", "0") != "1":
    @mcp.tool()
    def run_python(code: str) -> str:
        """Run arbitrary pyautogui/Python code in the guest."""
        return _ctrl.pyautogui(code) or "ok"


@mcp.tool()
def wait(seconds: float) -> str:
    time.sleep(min(seconds, 30))
    return f"waited {seconds}s"


# Grounding harness (config.GROUNDING / OSW_GROUNDING=1): three extra tools that resolve a NAMED
# target through the accessibility tree. Gated the same way run_python is above -- a tool the model
# is told about but cannot call is the setup that produced fabricated tool-call text in
# docs/finding-confabulation-under-tool-denial.md, so availability here and the prompt's tool list
# (prompts.GROUNDING_LINES) are driven by the same flag. The tools above, `click(x, y)` included,
# are untouched: this only ever adds a channel.
if os.environ.get("OSW_GROUNDING", "0") == "1":
    from benchmarks.osworld.mcp import grounding_tools

    grounding_tools.register(mcp, _ctrl)


if __name__ == "__main__":
    mcp.run()
