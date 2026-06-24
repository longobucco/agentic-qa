"""Prompt templates for the agent loop(s) and the (subscription-based) judge."""
from benchmarks.webvoyager.config import MAX_STEPS

AGENT_PROMPT = """You are an autonomous web agent completing ONE task on a LIVE website.

A Chrome browser is already running with CDP on port {port}. Control it ONLY through the
`agent-browser` CLI via the Bash tool. Core commands:
  agent-browser connect {port}                 # RUN THIS FIRST to attach to the browser
  agent-browser open <url>                      # navigate (https:// is auto-added)
  agent-browser snapshot                        # accessibility tree with refs like @e5
  agent-browser click @e5                       # click element by ref
  agent-browser fill @e5 "text"                 # clear + type into a field
  agent-browser type @e5 "text"                 # type without clearing
  agent-browser press Enter                     # press a key (Enter, Tab, ...)
  agent-browser scroll down 800                 # scroll (up/down/left/right)
  agent-browser get text @e5                    # read element text
  agent-browser screenshot {out}/step_N.png     # capture a screenshot

Procedure:
1. Run `agent-browser connect {port}` first.
2. Run `agent-browser open {start_url}`.
3. Loop: `snapshot` -> decide ONE next action -> execute -> re-`snapshot` (refs change
   after navigation/clicks). Dismiss cookie/consent banners if they cover the target
   (the snapshot/click error names the covering element).
4. Save screenshots as you progress ({out}/step_1.png, step_2.png, ...) and a final
   `agent-browser screenshot {out}/final.png` when you believe the task is done.
5. Keep it under ~{max_steps} browser actions. Do NOT ask for confirmation. Stay on the
   task's website.

Finish by printing your result on the LAST line, EXACTLY:
ANSWER: <concise answer to the question, or DONE for action-only tasks>

TASK: {ques}
START_URL: {start_url}
"""

ALUMNIUM_PROMPT = """You are an autonomous web agent completing ONE task on a LIVE website,
using the Alumnium MCP browser tools ONLY:
  mcp__alumnium__start_driver   - start a Chrome browser (call this FIRST)
  mcp__alumnium__do "<action>"  - perform an action in natural language (navigate/click/type/scroll)
  mcp__alumnium__check "<cond>" - verify a condition on the page (returns pass/fail)
  mcp__alumnium__get "<what>"   - extract data/text from the page
  mcp__alumnium__wait "<cond>"  - wait for a condition or delay
  mcp__alumnium__stop_driver    - close the browser (call this LAST)

Procedure:
1. start_driver.
2. do "navigate to {start_url}".
3. Use do / get / check to accomplish the task; re-check state as needed.
4. stop_driver.
5. Print your result on the LAST line, EXACTLY:
   ANSWER: <concise answer to the question, or DONE for action-only tasks>

Use ONLY the Alumnium MCP tools. Do NOT edit files or use other tools. Do not ask for
confirmation. Stay on the task's website.

TASK: {ques}
START_URL: {start_url}
"""

JUDGE_PROMPT = """You are evaluating whether a web agent completed a task (WebVoyager-style eval).

TASK: {ques}
WEBSITE: {web}
REFERENCE ANSWER (may be partial or outdated, use as a hint only): {ref}
AGENT'S FINAL ANSWER: {answer}
{evidence}
Judge SUCCESS only if the agent actually achieved the task goal. Be strict: a partial or
incorrect result is FAILURE; an unsupported/hallucinated answer is FAILURE.

Output ONLY one JSON object as the LAST line, nothing after it:
{{"verdict": "SUCCESS", "reason": "<one sentence>"}}
or
{{"verdict": "FAILURE", "reason": "<one sentence>"}}
"""

_EVIDENCE_SHOTS = (
    "\nThe agent's screenshots are in this directory: {out}\n"
    "Use the Read tool to view {out}/final.png and any {out}/step_*.png that exist; "
    "require screenshot evidence for the claimed result.\n"
)
_EVIDENCE_TEXT = (
    "\nNo screenshots are available; judge from the answer text against the reference.\n"
)


def agent_prompt(task, port, out):
    return AGENT_PROMPT.format(
        port=port,
        out=out,
        max_steps=MAX_STEPS,
        ques=task["ques"],
        start_url=task["web"],
    )


def alumnium_prompt(task, start_url=None):
    return ALUMNIUM_PROMPT.format(ques=task["ques"], start_url=start_url or task["web"])


def judge_prompt(task, answer, ref, out, has_screenshots=True):
    evidence = (_EVIDENCE_SHOTS.format(out=out) if has_screenshots else _EVIDENCE_TEXT)
    return JUDGE_PROMPT.format(
        ques=task["ques"],
        web=task.get("web", ""),
        ref=ref or "(none)",
        answer=answer or "(no answer produced)",
        evidence=evidence,
    )
