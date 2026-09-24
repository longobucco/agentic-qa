"""The official OSWorld Claude agent's computer tool, served over MCP (config.OFFICIAL).

Mirrors upstream step handling (mm_agents/anthropic/main.py + lib_run_single.py):
- each action is translated by upstream's own parse_actions_from_tool_call (vendored in
  _upstream_actions.py);
- the code is executed as upstream's PythonController does: POST /execute with
  ["python", "-c", PYAUTOGUI_PKGS_PREFIX.format(command=...)];
- the session pauses sleep_after_execution (0.5 s) after each action;
- every call answers "Success" plus an automatic screenshot plus "[Current step: N/M]".
A call is one step; a batched call is one step; the first screenshot is free (upstream hands
the agent the initial screenshot with the task). Beyond the budget nothing executes."""
import io
import time

from PIL import Image as PILImage

from benchmarks.osworld import config
from benchmarks.osworld.mcp._upstream_actions import (
    PYAUTOGUI_PKGS_PREFIX, parse_actions_from_tool_call)

PKGS_PREFIX = PYAUTOGUI_PKGS_PREFIX
_NO_EXEC = {"screenshot", "zoom", "done", "fail"}


def to_pyautogui(action_input):
    return parse_actions_from_tool_call({"input": action_input})


def _zoom(png, region):
    x0, y0, x1, y1 = (int(v) for v in region)
    if not (0 <= x0 < x1 <= config.SCREEN_WIDTH and 0 <= y0 < y1 <= config.SCREEN_HEIGHT):
        raise ValueError(f"zoom region {[x0, y0, x1, y1]} is not inside the screen")
    crop = PILImage.open(io.BytesIO(png)).crop((x0, y0, x1, y1))
    scale = min(config.SCREEN_WIDTH / (x1 - x0), config.SCREEN_HEIGHT / (y1 - y0))
    size = (max(1, int((x1 - x0) * scale)), max(1, int((y1 - y0) * scale)))
    buf = io.BytesIO()
    crop.resize(size, PILImage.Resampling.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()


class ComputerSession:
    def __init__(self, ctrl, *, max_steps, sleep_after=None, sleep=time.sleep):
        self.ctrl, self.max_steps, self.sleep = ctrl, max_steps, sleep
        self.sleep_after = config.SLEEP_AFTER_EXECUTION if sleep_after is None else sleep_after
        self.steps_used = 0
        self._screenshot_seen = False

    def _run_one(self, action):
        code = to_pyautogui(action)
        name = action.get("action")
        if name == "zoom":
            return _zoom(self.ctrl.screenshot(), action["region"])
        if name not in _NO_EXEC:
            self.ctrl.execute(["python", "-c", PKGS_PREFIX.format(command=code)],
                              timeout=120)
            self.sleep(self.sleep_after)
        return None

    def call(self, tool_input):
        free = (tool_input.get("action") == "screenshot" and not self._screenshot_seen
                and self.steps_used == 0)
        if not free:
            if self.steps_used >= self.max_steps:
                return [("text", f"Step limit reached ({self.max_steps}/{self.max_steps}). "
                                 f"No further actions are executed; stop now.")]
            self.steps_used += 1
        self._screenshot_seen = True
        actions = tool_input.get("actions") or [tool_input]
        texts, zoom_img = [], None
        for a in actions:
            try:
                img = self._run_one(a)
                zoom_img = img if img is not None else zoom_img
            except Exception as e:
                texts.append(f"Error in action {a.get('action')!r}: {type(e).__name__}: {e}")
                break
        image = zoom_img if zoom_img is not None else self.ctrl.screenshot()
        return ([("text", texts[0] if texts else "Success"), ("image", image),
                 ("text", f"[Current step: {self.steps_used}/{self.max_steps}]")])
