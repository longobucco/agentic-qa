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
4. Save a screenshot after each meaningful step ({out}/step_1.png, step_2.png, ...). The
   run is scored ONLY from screenshots, so capturing them is REQUIRED, not optional.
5. Keep it under ~{max_steps} browser actions. Do NOT ask for confirmation. Stay on the
   task's website.

Finish: when the task is done, FIRST capture the final state — REQUIRED, the run cannot be
scored without it:
  agent-browser screenshot {out}/final.png
Then print your result on the LAST line, EXACTLY:
ANSWER: <concise answer to the question, or DONE for action-only tasks>

TASK: {ques}
START_URL: {start_url}
"""

AGENT_DOCKER_PROMPT = """You are an autonomous web agent completing ONE task on a LIVE website.

A headless Chromium is ALREADY running and connected inside a Docker container named
`{cname}`. Control it ONLY by prefixing every `agent-browser` command with
`docker exec {cname}`, run via the Bash tool. Core commands:
  docker exec {cname} agent-browser open <url>            # navigate (https:// auto-added)
  docker exec {cname} agent-browser snapshot              # accessibility tree, refs like @e5
  docker exec {cname} agent-browser click @e5             # click element by ref
  docker exec {cname} agent-browser fill @e5 "text"       # clear + type into a field
  docker exec {cname} agent-browser type @e5 "text"       # type without clearing
  docker exec {cname} agent-browser press Enter           # press a key (Enter, Tab, ...)
  docker exec {cname} agent-browser scroll down 800       # scroll (up/down/left/right)
  docker exec {cname} agent-browser get text @e5          # read element text
  docker exec {cname} agent-browser screenshot /shots/step_N.png   # screenshot (see below)

Procedure:
1. The browser is already connected — start with
   `docker exec {cname} agent-browser open {start_url}`.
2. Loop: `snapshot` -> decide ONE next action -> execute -> re-`snapshot` (refs change after
   navigation/clicks). Dismiss cookie/consent banners if they cover the target.
3. Save a screenshot after each meaningful step to `/shots/step_1.png`, `/shots/step_2.png`,
   ... These live INSIDE the container at `/shots`; the harness collects them. The run is
   scored ONLY from screenshots, so capturing them is REQUIRED — always write under `/shots/`.
4. Keep it under ~{max_steps} browser actions. Do NOT ask for confirmation. Stay on the
   task's website.

Finish: when the task is done, FIRST capture the final state — REQUIRED, the run cannot be
scored without it:
  docker exec {cname} agent-browser screenshot /shots/final.png
Then print your result on the LAST line, EXACTLY:
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

JUDGE_PROMPT = """You are an evaluator scoring whether a web agent accomplished a task. This is
the canonical WebVoyager auto-evaluation, applied IDENTICALLY to every system under test.

You are given three things:
1. TASK — the natural-language instruction the agent was asked to perform.
2. RESULT RESPONSE — the agent's final textual answer.
3. RESULT SCREENSHOTS — image files saved in the directory {out}. Use the Read tool to view
   the LAST few screenshots there (the highest-numbered `step_*` files, and `final.png` if
   present). They are visual proof of the end state of the agent's actions.

Follow WebVoyager's evaluation protocol EXACTLY:
- Judge ONLY from the screenshots and the response. Do NOT use outside knowledge or assume
  facts not visible in the screenshots.
- The screenshot is authentic ground truth. If the RESULT RESPONSE contradicts the
  screenshot, the SCREENSHOT prevails. If the response states something NOT shown in the
  screenshot, you may believe the response.
- If the task has multiple parts, ALL parts must be satisfied or it is a failure.
- There is NO reference answer; decide from the evidence whether the intent was achieved.
- The current year is 2026; evaluate against the current web, not prior assumptions.
- If NO screenshots are present in {out}, you cannot verify the result -> FAILURE.

First Read the relevant screenshots in {out}, reason briefly, then output ONLY one JSON
object as the LAST line, nothing after it:
{{"verdict": "SUCCESS", "reason": "<one sentence>"}}
or
{{"verdict": "FAILURE", "reason": "<one sentence>"}}

TASK: {ques}
RESULT RESPONSE: {answer}
RESULT SCREENSHOTS: in {out}
"""


def agent_prompt(task, port, out):
    return AGENT_PROMPT.format(
        port=port,
        out=out,
        max_steps=MAX_STEPS,
        ques=task["ques"],
        start_url=task["web"],
    )


def agent_docker_prompt(task, cname):
    """Containerized agent prompt: drive agent-browser inside Steel container `cname` via
    `docker exec`. The runner has already started the container and connected the browser."""
    return AGENT_DOCKER_PROMPT.format(
        cname=cname,
        max_steps=MAX_STEPS,
        ques=task["ques"],
        start_url=task["web"],
    )


def alumnium_prompt(task, start_url=None):
    return ALUMNIUM_PROMPT.format(ques=task["ques"], start_url=start_url or task["web"])


def judge_prompt(task, answer, out):
    """Canonical WebVoyager judge prompt: task + answer + the screenshots under `out`.
    No reference answer (the benchmark judges from screenshots, not a gold string)."""
    return JUDGE_PROMPT.format(
        ques=task["ques"],
        answer=answer or "(no answer produced)",
        out=out,
    )
