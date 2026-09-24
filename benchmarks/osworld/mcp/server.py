"""OSWorld MCP server: the official `computer` tool, proxied to the in-guest controller. The
runner launches it per task with OSW_CONTROLLER_URL set. Needs the `mcp` package.

    OSW_CONTROLLER_URL=http://host:5000 python -m benchmarks.osworld.mcp.server
"""
import os

from mcp.server.fastmcp import FastMCP, Image

from benchmarks.osworld.env.controller import Controller
from benchmarks.osworld.mcp.official_computer import ComputerSession, write_state

mcp = FastMCP("osworld")
_ctrl = Controller(os.environ.get("OSW_CONTROLLER_URL", "http://localhost:5000"))

_session = ComputerSession(_ctrl, max_steps=int(os.environ.get("OSW_MAX_STEPS", "100")))
# The runner's proof that this server started, and its step count (OSW_MCP_STATE_FILE).
_STATE_FILE = os.environ.get("OSW_MCP_STATE_FILE", "")
write_state(_STATE_FILE, steps_used=0, max_steps=_session.max_steps)


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
    try:
        items = _session.call(inp)
    finally:
        write_state(_STATE_FILE, steps_used=_session.steps_used,
                    max_steps=_session.max_steps)
    return [Image(data=v, format="png") if k == "image" else v for k, v in items]


if __name__ == "__main__":
    mcp.run()
