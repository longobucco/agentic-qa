"""The official OSWorld Claude agent's computer tool, served over MCP.

Mirrors upstream step handling (mm_agents/anthropic/main.py, ~lines 554-600 and ~880-895, plus
lib_run_single.py):
- each action (or, in a batch, each sub-action) is translated by upstream's own
  parse_actions_from_tool_call (vendored in _upstream_actions.py);
- a single action's translated code is executed the way upstream's PythonController does:
  POST /execute with ["python", "-c", PYAUTOGUI_PKGS_PREFIX.format(command=...)] -- except
  "done"/"fail", which never execute;
- a BATCH first translates every sub-action (any translation error aborts the whole batch with
  nothing executed); only then are all the translated codes (again skipping "done"/"fail")
  concatenated into ONE combined command and run through ONE /execute + ONE sleep_after_execution
  pause -- never one execute per sub-action;
- zoom crops the screenshot taken AFTER that execution (its own code, "pyautogui.sleep(0.1)",
  still executes like any other action); in a batch, the zoom becomes the primary image, and if
  it isn't the batch's last sub-action the full screenshot is attached as a second image;
- a failed /execute is never retried on a timeout (retry_timeouts=False): a timed-out action may
  already have run its side effects on the guest, so resending it would replay them -- upstream's
  own PythonController.execute_python_command breaks on ReadTimeout the same way;
- every call answers "Success" (or an error) plus an automatic screenshot (best-effort: a failed
  screenshot degrades to no image rather than raising) plus "[Current step: N/M]".
A call is one step; a batched call is one step; the first screenshot is free (upstream hands
the agent the initial screenshot with the task). Beyond the budget nothing executes."""
import io
import json
import os
import time

from PIL import Image as PILImage

from benchmarks.osworld import config
from benchmarks.osworld.mcp._upstream_actions import (
    PYAUTOGUI_PKGS_PREFIX, parse_actions_from_tool_call)

PKGS_PREFIX = PYAUTOGUI_PKGS_PREFIX
_NO_EXEC = {"done", "fail"}


def to_pyautogui(action_input):
    # The vendored function assumes a well-formed action (e.g. a missing "action" key falls
    # through to tool_call.function.name, an AttributeError on our plain dict); every failure
    # mode -- upstream's own ValueError included -- surfaces here as ValueError, so callers have
    # one exception type to handle.
    try:
        return parse_actions_from_tool_call({"input": action_input})
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e


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


def write_state(path, *, steps_used, max_steps):
    """Liveness + step accounting for the runner (OSW_MCP_STATE_FILE, runners/common.py): written
    at server startup and after every `computer` call. Atomic (temp file + rename) so the runner
    never reads a half-written file. No-op when no path is configured."""
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"started": True, "steps_used": steps_used, "max_steps": max_steps}, f)
    os.replace(tmp, path)


class ComputerSession:
    def __init__(self, ctrl, *, max_steps, sleep_after=None, sleep=time.sleep):
        self.ctrl, self.max_steps, self.sleep = ctrl, max_steps, sleep
        self.sleep_after = config.SLEEP_AFTER_EXECUTION if sleep_after is None else sleep_after
        self.steps_used = 0
        self._screenshot_seen = False

    def _reminder(self):
        return f"[Current step: {self.steps_used}/{self.max_steps}]"

    def _safe_screenshot(self):
        # Best-effort: a screenshot failure degrades the response to text-only rather than
        # raising out of call() -- the action itself already ran (or was rejected) by this point.
        try:
            return self.ctrl.screenshot()
        except Exception:
            return None

    def _error_result(self, action_name, exc, *, img=None):
        items = [("text", f"Error in action {action_name!r}: {type(exc).__name__}: {exc}")]
        if img is not None:
            items.append(("image", img))
        items.append(("text", self._reminder()))
        return items

    def _execute(self, code):
        """POST the combined command once and pause -- shared by single actions and batches.
        Never retries a timeout (see the module docstring); other transient errors still retry
        inside Controller.execute."""
        self.ctrl.execute(["python", "-c", PKGS_PREFIX.format(command=code)],
                          timeout=120, retry_timeouts=False)
        self.sleep(self.sleep_after)

    def _call_single(self, action):
        name = action.get("action") if isinstance(action, dict) else None
        try:
            code = to_pyautogui(action)
        except ValueError as e:
            return self._error_result(name, e, img=self._safe_screenshot())

        if name not in _NO_EXEC:
            try:
                self._execute(code)
            except Exception as e:
                return self._error_result(name, e, img=self._safe_screenshot())

        if name == "zoom":
            shot = self._safe_screenshot()
            if shot is None:
                return [("text", "Success"), ("text", self._reminder())]
            try:
                image = _zoom(shot, action.get("region"))
            except Exception as e:
                return self._error_result(name, e, img=shot)
            return [("text", "Success"), ("image", image), ("text", self._reminder())]

        shot = self._safe_screenshot()
        if shot is None:
            return [("text", "Success"), ("text", self._reminder())]
        return [("text", "Success"), ("image", shot), ("text", self._reminder())]

    def _call_batch(self, actions):
        codes, zoom_action, zoom_index = [], None, None
        for idx, a in enumerate(actions):
            name = a.get("action") if isinstance(a, dict) else None
            try:
                if not isinstance(a, dict):
                    raise ValueError(f"batch sub-action must be an object, got {type(a).__name__}")
                code = to_pyautogui(a)
            except ValueError as e:
                # Nothing executes: translation happens for every sub-action BEFORE anything runs.
                return self._error_result(name, e, img=self._safe_screenshot())
            if name not in _NO_EXEC:
                codes.append(code)
            if name == "zoom":
                zoom_action, zoom_index = a, idx

        if codes:
            try:
                self._execute("".join(codes))
            except Exception as e:
                return self._error_result(None, e, img=self._safe_screenshot())

        shot = self._safe_screenshot()
        if shot is None:
            return [("text", "Success"), ("text", self._reminder())]

        if zoom_action is not None:
            try:
                zoom_img = _zoom(shot, zoom_action.get("region"))
            except Exception as e:
                return self._error_result("zoom", e, img=shot)
            items = [("text", "Success"), ("image", zoom_img)]
            if zoom_index < len(actions) - 1:
                items.append(("image", shot))
            items.append(("text", self._reminder()))
            return items

        return [("text", "Success"), ("image", shot), ("text", self._reminder())]

    def call(self, tool_input):
        free = (tool_input.get("action") == "screenshot" and not self._screenshot_seen
                and self.steps_used == 0)
        if not free:
            if self.steps_used >= self.max_steps:
                return [("text", f"Step limit reached ({self.max_steps}/{self.max_steps}). "
                                 f"No further actions are executed; stop now.")]
            self.steps_used += 1
        self._screenshot_seen = True

        actions = tool_input.get("actions")
        if isinstance(actions, list):
            return self._call_batch(actions)
        return self._call_single(tool_input)
