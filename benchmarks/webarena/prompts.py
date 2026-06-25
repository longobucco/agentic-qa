"""Prompt for the WebArena agent. There is no LLM judge here — scoring is deterministic — so
the agent just needs to (a) accomplish the intent on the hosted site and (b) state a final
answer for `string_match` tasks. The end STATE (final URL, DOM) is what gets checked."""
from benchmarks.webarena.config import MAX_TURNS

AGENT_DOCKER_PROMPT = """You are an autonomous web agent completing ONE task on a self-hosted
website. A headless Chromium is ALREADY running and connected inside a Docker container named
`{cname}`. Control it ONLY by prefixing every `agent-browser` command with `docker exec {cname}`:
  docker exec {cname} agent-browser open <url>
  docker exec {cname} agent-browser snapshot              # accessibility tree, refs like @e5
  docker exec {cname} agent-browser click @e5
  docker exec {cname} agent-browser fill @e5 "text"
  docker exec {cname} agent-browser press Enter
  docker exec {cname} agent-browser get text @e5

Procedure:
1. `docker exec {cname} agent-browser open {start_url}`.
2. Loop: snapshot -> ONE action -> execute -> re-snapshot, until the task is done. Actually
   PERFORM the requested change (add to cart, post, edit, navigate) — the site's resulting
   STATE is what's graded, not your narration.
3. Keep it under ~{max_steps} actions. Do NOT ask for confirmation.

Finish by printing on the LAST line EXACTLY:
ANSWER: <the requested information, or N/A for pure action tasks>

TASK: {intent}
START_URL: {start_url}
"""


def agent_docker_prompt(task, cname):
    return AGENT_DOCKER_PROMPT.format(
        cname=cname, max_steps=MAX_TURNS,
        intent=task["intent"], start_url=task.get("start_url", ""),
    )
