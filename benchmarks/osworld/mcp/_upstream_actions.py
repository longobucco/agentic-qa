"""GENERATED from xlang-ai/OSWorld@091f5ef1d5544bc74953c77875d5feb5bed30108:
mm_agents/anthropic/main.py::parse_actions_from_tool_call (self removed, resize
disabled: screenshots are delivered at native 1920x1080) and
desktop_env/controllers/python.py::PYAUTOGUI_PKGS_PREFIX. Do not edit by hand."""
from typing import Dict

_RESIZE_FACTOR = None

PYAUTOGUI_PKGS_PREFIX = (
    "import pyautogui; import time; import platform; "
    "pyautogui.FAILSAFE = False; "
    "_osworld_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\"<>?'; "
    "_osworld_linux_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\">?'; "
    "pyautogui.isShiftCharacter = lambda character: character.isupper() or "
    "character in (_osworld_linux_shift_chars if platform.system() == 'Linux' else _osworld_shift_chars); "
    "{command}"
)


def parse_actions_from_tool_call(tool_call: Dict) -> str:
    result = ""
    function_args = (
        tool_call["input"]
    )

    action = function_args.get("action")
    if not action:
        action = tool_call.function.name
    action_conversion = {
        "left click": "click",
        "right click": "right_click"
    }
    action = action_conversion.get(action, action)
    
    text = function_args.get("text")
    coordinate = function_args.get("coordinate")
    start_coordinate = function_args.get("start_coordinate")
    scroll_direction = function_args.get("scroll_direction")
    scroll_amount = function_args.get("scroll_amount")
    duration = function_args.get("duration")
    # "repeat" (key presses) is part of the batched-action schema and the native
    # computer_20251124 tool; absent or null means press once.
    repeat = int(function_args.get("repeat") or 1)

    # resize coordinates if resize_factor is set
    if coordinate and _RESIZE_FACTOR:
        coordinate = (
            int(coordinate[0] * _RESIZE_FACTOR[0]),
            int(coordinate[1] * _RESIZE_FACTOR[1])
        )
    if start_coordinate and _RESIZE_FACTOR:
        start_coordinate = (
            int(start_coordinate[0] * _RESIZE_FACTOR[0]),
            int(start_coordinate[1] * _RESIZE_FACTOR[1])
        )
    
    if action == "left_mouse_down":
        result += "pyautogui.mouseDown()\n"
    elif action == "left_mouse_up":
        result += "pyautogui.mouseUp()\n"
    
    elif action == "hold_key":
        if not isinstance(text, str):
            raise ValueError(f"{text} must be a string")
        
        keys = text.split('+')
        for key in keys:
            key = key.strip().lower()
            result += f"pyautogui.keyDown('{key}')\n"
        expected_outcome = f"Keys {text} held down."

    # Handle mouse move and drag actions
    elif action in ("mouse_move", "left_click_drag"):
        if coordinate is None:
            raise ValueError(f"coordinate is required for {action}")
        if text is not None:
            raise ValueError(f"text is not accepted for {action}")
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            raise ValueError(f"{coordinate} must be a tuple of length 2")
        if not all(isinstance(i, int) for i in coordinate):
            raise ValueError(f"{coordinate} must be a tuple of ints")
        
        x, y = coordinate[0], coordinate[1]
        if action == "mouse_move":
            result += (
                f"pyautogui.moveTo({x}, {y}, duration={duration or 0.5})\n"
            )
            expected_outcome = f"Mouse moved to ({x},{y})."
        elif action == "left_click_drag":
            # If start_coordinate is provided, validate and move to start before dragging
            if start_coordinate:
                if not isinstance(start_coordinate, (list, tuple)) or len(start_coordinate) != 2:
                    raise ValueError(f"{start_coordinate} must be a tuple of length 2")
                if not all(isinstance(i, int) for i in start_coordinate):
                    raise ValueError(f"{start_coordinate} must be a tuple of ints")
                start_x, start_y = start_coordinate[0], start_coordinate[1]
                result += (
                    f"pyautogui.moveTo({start_x}, {start_y}, duration={duration or 0.5})\n"
                )
            result += (
                f"pyautogui.dragTo({x}, {y}, duration={duration or 0.5})\n"
            )
            expected_outcome = f"Cursor dragged to ({x},{y})."

    # Handle keyboard actions
    elif action in ("key", "type"):
        if text is None:
            raise ValueError(f"text is required for {action}")
        if coordinate is not None:
            raise ValueError(f"coordinate is not accepted for {action}")
        if not isinstance(text, str):
            raise ValueError(f"{text} must be a string")

        if action == "key":
            key_conversion = {
                "page_down": "pagedown",
                "page_up": "pageup",
                "super_l": "win",
                "super": "command",
                "escape": "esc"
            }
            keys = text.split('+')
            for _ in range(repeat):
                for key in keys:
                    key = key.strip().lower()
                    key = key_conversion.get(key, key)
                    result += (f"pyautogui.keyDown('{key}')\n")
                for key in reversed(keys):
                    key = key.strip().lower()
                    key = key_conversion.get(key, key)
                    result += (f"pyautogui.keyUp('{key}')\n")
            expected_outcome = f"Key {text} pressed {repeat} time(s)."
        elif action == "type":
            for char in text:
                if char == '\n':
                    result += "pyautogui.press('enter')\n"
                elif char == "'":
                    result += 'pyautogui.press("\'")\n'
                elif char == '\\':
                    result += "pyautogui.press('\\\\')\n"
                elif char == '"':
                    result += "pyautogui.press('\"')\n"
                else:
                    result += f"pyautogui.press('{char}')\n"
            expected_outcome = f"Text {text} written."

    # Handle scroll actions
    elif action == "scroll":
        if text is not None:
            result += (f"pyautogui.keyDown('{text.lower()}')\n")
        if coordinate is None:
            if scroll_direction in ("up", "down"):
                result += (
                    f"pyautogui.scroll({scroll_amount if scroll_direction == 'up' else -scroll_amount})\n"
                )
            elif scroll_direction in ("left", "right"):
                result += (
                    f"pyautogui.hscroll({scroll_amount if scroll_direction == 'right' else -scroll_amount})\n"
                )
        else:
            if scroll_direction in ("up", "down"):
                x, y = coordinate[0], coordinate[1]
                result += (
                    f"pyautogui.scroll({scroll_amount if scroll_direction == 'up' else -scroll_amount}, {x}, {y})\n"
                )
            elif scroll_direction in ("left", "right"):
                x, y = coordinate[0], coordinate[1]
                result += (
                    f"pyautogui.hscroll({scroll_amount if scroll_direction == 'right' else -scroll_amount}, {x}, {y})\n"
                )
        if text is not None:
            result += (f"pyautogui.keyUp('{text.lower()}')\n")
        expected_outcome = "Scroll action finished"

    # Handle click actions
    elif action in ("left_click", "right_click", "double_click", "middle_click", "left_press", "triple_click"):
        # Handle modifier keys during click if specified
        if text:
            keys = text.split('+')
            for key in keys:
                key = key.strip().lower()
                result += f"pyautogui.keyDown('{key}')\n"
        if coordinate is not None:
            x, y = coordinate
            if action == "left_click":
                result += (f"pyautogui.click({x}, {y})\n")
            elif action == "right_click":
                result += (f"pyautogui.rightClick({x}, {y})\n")
            elif action == "double_click":
                result += (f"pyautogui.doubleClick({x}, {y})\n")
            elif action == "middle_click":
                result += (f"pyautogui.middleClick({x}, {y})\n")
            elif action == "left_press":
                result += (f"pyautogui.mouseDown({x}, {y})\n")
                result += ("time.sleep(1)\n")
                result += (f"pyautogui.mouseUp({x}, {y})\n")
            elif action == "triple_click":
                result += (f"pyautogui.tripleClick({x}, {y})\n")

        else:
            if action == "left_click":
                result += ("pyautogui.click()\n")
            elif action == "right_click":
                result += ("pyautogui.rightClick()\n")
            elif action == "double_click":
                result += ("pyautogui.doubleClick()\n")
            elif action == "middle_click":
                result += ("pyautogui.middleClick()\n")
            elif action == "left_press":
                result += ("pyautogui.mouseDown()\n")
                result += ("time.sleep(1)\n")
                result += ("pyautogui.mouseUp()\n")
            elif action == "triple_click":
                result += ("pyautogui.tripleClick()\n")
        # Release modifier keys after click
        if text:
            keys = text.split('+')
            for key in reversed(keys):
                key = key.strip().lower()
                result += f"pyautogui.keyUp('{key}')\n"
        expected_outcome = "Click action finished"
        
    elif action == "wait":
        result += "pyautogui.sleep(0.5)\n"
        expected_outcome = "Wait for 0.5 seconds"
    elif action == "fail":
        result += "FAIL"
        expected_outcome = "Finished"
    elif action == "done":
        result += "DONE"
        expected_outcome = "Finished"
    elif action == "call_user":
        result += "CALL_USER"
        expected_outcome = "Call user"
    elif action == "screenshot":
        result += "pyautogui.sleep(0.1)\n"
        expected_outcome = "Screenshot taken"
    elif action == "zoom":
        # Zoom executes like screenshot but with special region handling
        result += "pyautogui.sleep(0.1)\n"
        expected_outcome = "Zoom screenshot taken"
    else:
        raise ValueError(f"Invalid action: {action}")
    
    return result
