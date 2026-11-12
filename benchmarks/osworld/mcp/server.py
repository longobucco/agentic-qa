"""OSWorld MCP server: the official `computer` tool, proxied to the in-guest controller, plus the
tools a declared arm adds (OSW_ARM_TOOLS). The runner launches it per task with OSW_CONTROLLER_URL
set. Needs the `mcp` package.

    OSW_CONTROLLER_URL=http://host:5000 python -m benchmarks.osworld.mcp.server
"""
import json
import os

from mcp.server.fastmcp import FastMCP, Image

from benchmarks.osworld.env.controller import Controller
from benchmarks.osworld.mcp import command_tool
from benchmarks.osworld.mcp.official_computer import ComputerSession, write_state

mcp = FastMCP("osworld")
_ctrl = Controller(os.environ.get("OSW_CONTROLLER_URL", "http://localhost:5000"))

_session = ComputerSession(_ctrl, max_steps=int(os.environ.get("OSW_MAX_STEPS", "100")))
# The runner's proof that this server started, and its step count (OSW_MCP_STATE_FILE).
_STATE_FILE = os.environ.get("OSW_MCP_STATE_FILE", "")


def _steps_already_used():
    """The steps this run has already spent, which a new server process must not hand back.

    Two ways a second process can serve the same budget. An arm's bounce resumes deliberately and
    the runner passes OSW_STEPS_USED_START. The other is not deliberate: this server is a stdio
    subprocess of the CLI, so if it dies the client restarts it, and a fresh ComputerSession
    starts at 0 -- the agent silently gets another 100 steps. Measured once in 1,542 archived
    runs (campaign_analysis/call_delivery.py), with 3 steps handed back and no run near the cap,
    but the budget is a declared constant of the protocol and must not depend on a crash.

    The state file is the carry-over: `common.reset_mcp_state` deletes it before the agent
    starts, so a file holding steps for THIS run dir can only have been written by this attempt.
    """
    start = int(os.environ.get("OSW_STEPS_USED_START", "0") or 0)
    try:
        with open(_STATE_FILE) as f:
            prior = json.load(f)
        if prior.get("started"):
            start = max(start, int(prior.get("steps_used") or 0))
    except (OSError, ValueError, TypeError):
        pass
    return start


_session.steps_used = _steps_already_used()
# A restart that resumes mid-budget has already seen its free first screenshot: charge for the
# next one, or the reset would still hand back a step.
_session._screenshot_seen = _session.steps_used > 0
write_state(_STATE_FILE, steps_used=_session.steps_used, max_steps=_session.max_steps)


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


_COMMAND_LOG = os.environ.get("OSW_COMMAND_LOG", "")

# Only under an arm that declares it (OSW_ARM_TOOLS, set by the runner from arms.tools).
if "run_command" in os.environ.get("OSW_ARM_TOOLS", "").split(","):
    @mcp.tool()
    def run_command(command: str, timeout: int = command_tool.DEFAULT_TIMEOUT_S) -> str:
        """Run one bash command inside the Ubuntu desktop and return its exit code, stdout and
        stderr. timeout: seconds before the command is killed (default 60, at most 110). Each
        call counts as one step, like a computer action."""
        try:
            return command_tool.run_command(_session, _ctrl, command, timeout,
                                            log_path=_COMMAND_LOG)
        finally:
            write_state(_STATE_FILE, steps_used=_session.steps_used,
                        max_steps=_session.max_steps)


if __name__ == "__main__":
    mcp.run()
