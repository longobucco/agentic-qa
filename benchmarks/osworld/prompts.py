"""Prompt for the OSWorld desktop agent."""
from benchmarks.osworld.config import (
    GROUNDING, MAX_STEPS, OBSERVATION, RESTRICT_RUN_PYTHON, SELF_VERIFY,
)

# G5 idea #11 (docs/g5-arm-restrict-run-python-plan.md): when RESTRICT_RUN_PYTHON also denies the
# tool at the CLI level (runners/agent_computer._extra_flags), this line must be dropped too --
# advertising a tool the model then finds genuinely absent mid-task is exactly the setup that
# produced fabricated tool-call text in docs/finding-confabulation-under-tool-denial.md. Never
# promise a capability this run doesn't actually have, same principle as Bash/WebSearch/WebFetch
# never appearing here even though config.ENFORCE_SANDBOX denies those too.
RUN_PYTHON_LINE = "  run_python(code)        -> run arbitrary pyautogui code (escape hatch)\n"

# Grounding harness (config.GROUNDING, docs/grounding-harness-plan.md). Advertised only when
# mcp/server.py actually registers the tools -- same rule as RUN_PYTHON_LINE above, and for the
# same reason (docs/finding-confabulation-under-tool-denial.md).
GROUNDING_LINES = """  find_element(description, role="")    -> where a NAMED element is, without clicking
  click_element(description, role="")   -> click a NAMED element, and report if nothing changed
  list_elements(role="", name_contains="") -> the interactive elements currently on screen
"""

# Deliberately short, and framed as "when", not "always". Forcing every click through the tree
# would be the wrong intervention: a11y trees are incomplete on custom-rendered surfaces (GIMP's
# canvas, an image viewer), so coordinates stay first-class for anything without a name. The
# measured problem this addresses is narrower -- re-clicks within 8px of the previous click run at
# 10.9% of clicks on tasks that never pass vs 8.3% on tasks that always do
# (analysis/g10_grounding_signal.py) -- i.e. targeting named controls by eye.
GROUNDING_BLOCK = """
Targeting: when the thing you want to act on has a NAME in the interface (a button, a menu entry,
a cell reference, a field label), use click_element("that name") instead of estimating coordinates
from the screenshot. It resolves the name through the accessibility tree, which is exact, and it
tells you whether the click actually changed anything. If it reports the screen did not change,
do NOT repeat the same call -- try rank=2, describe the target differently, or switch to
screenshot() plus click(x, y). If nothing resolves at all, the element is not in the tree (common
on drawing canvases and image editors): read the screenshot and click coordinates, as normal.
Use list_elements() when you do not yet know what a control is called.
"""

AGENT_PROMPT = """You are an autonomous agent operating a real Linux desktop to complete ONE task.
You control the computer ONLY through these MCP tools (there is NO browser and NO shell):
  screenshot()            -> a PNG of the current screen (your primary observation)
  a11y_tree()             -> the accessibility tree (element roles, names, positions)
  click(x, y) / double_click(x, y) / right_click(x, y)
  move(x, y) / scroll(dx, dy)
  type(text)              -> type a string at the current focus
  key("ctrl+s")           -> press a key combination
{run_python_line}{grounding_lines}  wait(seconds)

Procedure:
1. Call screenshot() (and a11y_tree() when you need precise coordinates) to observe the desktop.
2. Loop: observe -> ONE action -> execute -> re-observe, until done. Actually perform the change;
   the resulting STATE is graded, not your narration.
3. Keep it under ~{max_steps} actions. Do NOT ask for confirmation. Dismiss dialogs yourself.
{grounding}{self_verify}
Finish by printing on the LAST line EXACTLY:
ANSWER: <the requested information, or DONE for pure action tasks>
If the task itself is impossible to complete as stated (contradictory, missing a required
precondition, asks for something that does not exist), do not attempt a workaround -- print:
ANSWER: FAIL

Observation profile: {observation}.

TASK: {instruction}
"""

# G5 ARM_SELF_VERIFY (docs/g5-arm-self-verify-plan.md): kept short deliberately -- long enough to
# force a genuine re-observation step, not so long it reads as generic boilerplate the model
# learns to skim past.
SELF_VERIFY_BLOCK = """
Before your final answer: take one more screenshot() and re-check the CURRENT desktop state
against every part of the instruction, one item at a time. Do not rely on your memory of having
done something -- verify it is actually true in this new screenshot. Print DONE only if
everything you just re-checked holds. If something is missing or wrong and you still have room
to fix it, fix it first. If the instruction is impossible given what you now observe (not just
what it looked like at the start), print FAIL -- a correct FAIL is not a worse answer than an
incorrect DONE.
"""


def agent_prompt(task):
    return AGENT_PROMPT.format(
        max_steps=MAX_STEPS, observation=OBSERVATION, instruction=task["instruction"],
        self_verify=SELF_VERIFY_BLOCK if SELF_VERIFY else "",
        run_python_line="" if RESTRICT_RUN_PYTHON else RUN_PYTHON_LINE,
        grounding_lines=GROUNDING_LINES if GROUNDING else "",
        grounding=GROUNDING_BLOCK if GROUNDING else "",
    )
