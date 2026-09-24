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

# Official-fidelity protocol (docs/superpowers/plans/2026-09-24-osworld-official-fidelity.md,
# config.OFFICIAL/config.PROTOCOL): under OSW_PROTOCOL=official the server exposes exactly one
# tool, `computer`, mirroring upstream's single-tool action surface -- none of the baseline
# tools below (or the gated extra channels: run_python, grounding, zoom/batch) are registered
# alongside it. Read directly from the environment (not benchmarks.osworld.config) so this
# module has no import-time dependency on the rest of the package's config validation.
_OFFICIAL = os.environ.get("OSW_PROTOCOL", "") == "official"


def _tool(*args, **kwargs):
    """`mcp.tool()`, except a no-op decorator under the official protocol. Every baseline
    function below stays defined at plain module-level top indentation either way (so
    test_readonly_mcp.py's byte-for-byte source comparison against readonly_server.py's own
    screenshot/a11y_tree/wait, and test_mcp_server.py's direct `server.click(...)` calls, are
    both unaffected by _OFFICIAL) -- only whether it becomes an MCP tool changes."""
    if _OFFICIAL:
        return lambda fn: fn
    return mcp.tool(*args, **kwargs)


@_tool()
def screenshot() -> Image:
    """PNG of the current screen."""
    return Image(data=_ctrl.screenshot(), format="png")


@_tool()
def a11y_tree() -> str:
    """Accessibility tree, for grounding."""
    return _ctrl.a11y_tree()


@_tool()
def click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.click({x}, {y})")
    return f"clicked ({x}, {y})"


@_tool()
def double_click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.doubleClick({x}, {y})")
    return f"double-clicked ({x}, {y})"


@_tool()
def right_click(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.rightClick({x}, {y})")
    return f"right-clicked ({x}, {y})"


@_tool()
def move(x: int, y: int) -> str:
    _ctrl.pyautogui(f"import pyautogui; pyautogui.moveTo({x}, {y})")
    return f"moved to ({x}, {y})"


@_tool()
def scroll(dx: int, dy: int) -> str:
    code = "import pyautogui; "
    if dy:
        code += f"pyautogui.scroll({dy}); "
    if dx:
        code += f"pyautogui.hscroll({dx})"
    _ctrl.pyautogui(code)
    return f"scrolled dx={dx} dy={dy}"


@_tool(name="type")
def type_text(text: str) -> str:
    # json.dumps produces a fully-escaped double-quoted literal (newlines, quotes,
    # backslashes, unicode) -- a manual .replace() missed newlines, which broke any
    # multi-line text as a syntax error in the generated source (see the guest's /run_python)
    _ctrl.pyautogui(f"import pyautogui; pyautogui.write({json.dumps(text)}, interval=0.02)")
    return f"typed {len(text)} chars"


@_tool()
def key(keys: str) -> str:
    """Press a combo, e.g. 'ctrl+s'."""
    tokens = json.dumps([k.strip() for k in keys.split("+")])
    _ctrl.pyautogui(f"import pyautogui; pyautogui.hotkey(*{tokens})")
    return f"pressed {keys}"


if os.environ.get("OSW_RESTRICT_RUN_PYTHON", "0") != "1":
    @_tool()
    def run_python(code: str) -> str:
        """Run arbitrary pyautogui/Python code in the guest."""
        return _ctrl.pyautogui(code) or "ok"


@_tool()
def wait(seconds: float) -> str:
    time.sleep(min(seconds, 30))
    return f"waited {seconds}s"


# Grounding harness (config.GROUNDING / OSW_GROUNDING=1): three extra tools that resolve a NAMED
# target through the accessibility tree. Gated the same way run_python is above -- a tool the
# model is told about but cannot call is the setup that produced fabricated tool-call text in
# docs/finding-confabulation-under-tool-denial.md, so availability here and the prompt's tool list
# (prompts.GROUNDING_LINES) are driven by the same flag. The tools above, `click(x, y)` included,
# are untouched: this only ever adds a channel. Never alongside the official protocol (exactly one
# tool, `computer`, per the spec).
if not _OFFICIAL and os.environ.get("OSW_GROUNDING", "0") == "1":
    from benchmarks.osworld.mcp import grounding_tools

    grounding_tools.register(mcp, _ctrl)


# Zoom + batched actions (config.ZOOM_BATCH, docs/superpowers/plans/2026-09-24-osworld-protocol-
# alignment.md Task 4): same gating rule as run_python and grounding above.
if not _OFFICIAL and os.environ.get("OSW_ZOOM_BATCH", "0") == "1":
    from benchmarks.osworld.mcp import zoom_batch_tools

    zoom_batch_tools.register(mcp, _ctrl)


if _OFFICIAL:
    from benchmarks.osworld.mcp.official_computer import ComputerSession

    _session = ComputerSession(_ctrl, max_steps=int(os.environ.get("OSW_MAX_STEPS", "100")))

    @mcp.tool()
    def computer(action: str = "", coordinate: list[int] | None = None,
                 start_coordinate: list[int] | None = None, text: str | None = None,
                 scroll_direction: str | None = None, scroll_amount: int | None = None,
                 duration: float | None = None, repeat: int | None = None,
                 region: list[int] | None = None, actions: list[dict] | None = None) -> list:
        """Use the mouse and keyboard to interact with the Ubuntu desktop and take screenshots
        (1920x1080, coordinates in screenshot pixels). action is one of: screenshot, zoom
        (region=[x0,y0,x1,y1]), left_click, right_click, middle_click, double_click,
        triple_click (coordinate=[x,y], optional text=modifier keys), mouse_move, left_click_drag
        (start_coordinate, coordinate), left_mouse_down, left_mouse_up, scroll (scroll_direction
        up/down/left/right, scroll_amount, optional coordinate), type (text), key (text like
        'ctrl+s', optional repeat), hold_key (text), wait. Pass actions=[{...}, ...] instead to
        run several actions in one call. Every call returns a screenshot."""
        inp = {k: v for k, v in dict(
            action=action, coordinate=coordinate, start_coordinate=start_coordinate, text=text,
            scroll_direction=scroll_direction, scroll_amount=scroll_amount, duration=duration,
            repeat=repeat, region=region, actions=actions).items() if v not in (None, "")}
        return [Image(data=v, format="png") if k == "image" else v
                for k, v in _session.call(inp)]


if __name__ == "__main__":
    mcp.run()
