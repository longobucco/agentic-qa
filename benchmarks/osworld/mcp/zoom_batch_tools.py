"""Zoom and batched actions for the OSWorld MCP server (config.ZOOM_BATCH / OSW_ZOOM_BATCH=1).

WHY. The published OSWorld-Verified score for Claude Sonnet 5 (System Card §8.1 note 14, §8.10.2)
came from a harness whose computer tool offers a zoom action and batched actions; our MCP toolset
had neither. These two tools add the same two capabilities generically -- no per-app or per-task
logic -- and are offered identically to Claude Code and Codex, so the Sonnet/Astra comparison
stays like-for-like. Registered only when the flag is on (same rule as run_python/grounding in
server.py: never advertise a tool the run does not have); the arm writes to its own results tree.

Coordinates stay in full-screen pixel space everywhere, also after a zoom.
The plain functions take a Controller, so they are testable without the `mcp` package.
"""
import io
import json
import time

from PIL import Image as PILImage

from benchmarks.osworld import config

MAX_BATCH = 20
MAX_WAIT_S = 30
NOT_EXECUTED = "not executed: an earlier action in this batch failed"
_POINTER = {"click": "click", "double_click": "doubleClick", "right_click": "rightClick",
            "move": "moveTo"}


def _xy(x, y):
    x, y = int(x), int(y)
    if not (0 <= x < config.SCREEN_WIDTH and 0 <= y < config.SCREEN_HEIGHT):
        raise ValueError(f"({x}, {y}) is outside the screen "
                         f"{config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}")
    return x, y


def zoom_png(ctrl, x0, y0, x1, y1):
    x0, y0, x1, y1 = (int(v) for v in (x0, y0, x1, y1))
    if not (0 <= x0 < x1 <= config.SCREEN_WIDTH and 0 <= y0 < y1 <= config.SCREEN_HEIGHT):
        raise ValueError(f"region ({x0}, {y0}, {x1}, {y1}) is not a non-empty rectangle inside "
                         f"the screen {config.SCREEN_WIDTH}x{config.SCREEN_HEIGHT}")
    crop = PILImage.open(io.BytesIO(ctrl.screenshot())).crop((x0, y0, x1, y1))
    scale = min(config.SCREEN_WIDTH / (x1 - x0), config.SCREEN_HEIGHT / (y1 - y0))
    size = (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)))
    buf = io.BytesIO()
    crop.resize(size, PILImage.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()


def _code(action):
    kind = action.get("action")
    if kind in _POINTER:
        x, y = _xy(action["x"], action["y"])
        return f"import pyautogui; pyautogui.{_POINTER[kind]}({x}, {y})"
    if kind == "scroll":
        dx, dy = int(action.get("dx", 0)), int(action.get("dy", 0))
        code = "import pyautogui; "
        if dy:
            code += f"pyautogui.scroll({dy}); "
        if dx:
            code += f"pyautogui.hscroll({dx})"
        return code
    if kind == "type":
        return f"import pyautogui; pyautogui.write({json.dumps(str(action['text']))}, interval=0.02)"
    if kind == "key":
        tokens = json.dumps([k.strip() for k in str(action["keys"]).split("+")])
        return f"import pyautogui; pyautogui.hotkey(*{tokens})"
    raise ValueError(f"unsupported action {kind!r}")


def _guest_error(response):
    """The guest's /run_python answers JSON with a status; anything but an explicit error is ok."""
    try:
        body = json.loads(response)
    except (TypeError, ValueError):
        return None
    if isinstance(body, dict) and body.get("status") == "error":
        return body.get("message") or body.get("error") or "guest reported an error"
    return None


def run_batch(ctrl, actions, *, sleep=time.sleep):
    if not isinstance(actions, list) or not actions:
        return "error: actions must be a non-empty list"
    if len(actions) > MAX_BATCH:
        return (f"error: at most {MAX_BATCH} actions per batch, got {len(actions)} -- "
                f"nothing executed")
    lines, failed = [], False
    for i, action in enumerate(actions, 1):
        if failed:
            lines.append(f"{i}. {NOT_EXECUTED}")
            continue
        try:
            if not isinstance(action, dict):
                raise ValueError("each action must be an object")
            if action.get("action") == "wait":
                seconds = min(float(action.get("seconds", 1)), MAX_WAIT_S)
                sleep(seconds)
                lines.append(f"{i}. ok: wait")
                continue
            err = _guest_error(ctrl.pyautogui(_code(action)))
            if err:
                raise RuntimeError(err)
            lines.append(f"{i}. ok: {action['action']}")
        except Exception as e:
            failed = True
            lines.append(f"{i}. error: {type(e).__name__}: {e}")
    return "\n".join(lines)


def register(mcp, ctrl):
    from mcp.server.fastmcp import Image

    @mcp.tool()
    def zoom(x0: int, y0: int, x1: int, y1: int) -> Image:
        """PNG of the screen region (x0, y0)-(x1, y1), enlarged to fit the screen. Coordinates
        for clicks stay in full-screen pixels."""
        return Image(data=zoom_png(ctrl, x0, y0, x1, y1), format="png")

    @mcp.tool()
    def batch(actions: list[dict]) -> str:
        """Run up to 20 actions in order, stopping at the first failure. Each action is an
        object: {"action": "click"|"double_click"|"right_click"|"move", "x", "y"} |
        {"action": "scroll", "dx", "dy"} | {"action": "type", "text"} |
        {"action": "key", "keys"} | {"action": "wait", "seconds"}."""
        return run_batch(ctrl, actions)
