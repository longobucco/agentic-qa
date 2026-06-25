"""Prompt templates for the BrowseComp research agent and the answer-vs-reference grader."""
from benchmarks.browsecomp.config import MAX_TURNS

# The agent drives agent-browser inside a Steel container (via `docker exec {cname}`), exactly
# like WebVoyager's docker path, but the task is RESEARCH: there is no start URL — the agent
# searches the open web to find a short factual answer. BrowseComp questions are designed to
# be hard for plain search, so the agent must dig (follow links, cross-check).
AGENT_DOCKER_PROMPT = """You are an autonomous research agent answering ONE hard question by
browsing the LIVE web. A headless Chromium is ALREADY running and connected inside a Docker
container named `{cname}`. Control it ONLY by prefixing every `agent-browser` command with
`docker exec {cname}`, run via the Bash tool. Core commands:
  docker exec {cname} agent-browser open <url>            # navigate (https:// auto-added)
  docker exec {cname} agent-browser snapshot              # accessibility tree, refs like @e5
  docker exec {cname} agent-browser click @e5             # click element by ref
  docker exec {cname} agent-browser fill @e5 "text"       # type into a field (e.g. search box)
  docker exec {cname} agent-browser press Enter
  docker exec {cname} agent-browser get text @e5          # read element text
  docker exec {cname} agent-browser eval "document.body.innerText"   # read page text

Approach:
1. There is NO given URL. Start from a search engine — prefer `https://duckduckgo.com/html/?q=...`
   (headless-friendly; Google often shows a bot wall). URL-encode your query.
2. Read results, open promising pages, and CROSS-CHECK facts — these questions are hard and
   the first hit is often wrong or incomplete. Reformulate queries as needed.
3. Keep it under ~{max_steps} browser actions. Do NOT ask for confirmation.

Finish with EXACTLY two lines at the very end:
ANSWER: <your concise final answer, or UNKNOWN if you could not determine it>
CONFIDENCE: <an integer 0-100, your confidence the answer is correct>

QUESTION: {ques}
"""

# PLAIN Claude Code (no agent-browser) — the A/B comparator that isolates the browser harness:
# same orchestrator + model as the agent_browser runner, but research is done with Claude Code's
# OWN native tools (WebSearch / WebFetch) instead of driving a real browser. agent_browser vs
# claude_code = "with vs without the browser harness," holding the model fixed.
CLAUDE_CODE_PROMPT = """You are a research agent answering ONE deliberately-hard question. Use
your built-in tools — WebSearch to find sources and WebFetch to read them — to research the
LIVE web. These questions resist plain search: the first hit is often wrong or incomplete, so
issue multiple searches, open primary sources, and CROSS-CHECK before committing. Do NOT ask
for confirmation; do NOT stop until you have an answer or have exhausted reasonable searches.

Finish with EXACTLY two lines at the very end:
ANSWER: <your concise final answer, or UNKNOWN if you could not determine it>
CONFIDENCE: <an integer 0-100, your confidence the answer is correct>

QUESTION: {ques}
"""

# Answer-vs-reference grader — an LLM judge on `core.judge` plumbing, NO screenshots. This is
# the second judge implementation (WebVoyager's is the screenshot judge): same `claude -p`
# verdict parsing, entirely different evidence (text only).
GRADER_PROMPT = """You are grading whether a research agent's answer to a question is correct,
by comparing it to the known reference answer. Judge ONLY semantic correctness:
- Exact wording is NOT required; an answer that means the same thing is correct.
- For names/dates/numbers, the value must match the reference (a date range must cover it,
  a number must be equal modulo formatting).
- If the agent answered UNKNOWN, gave no answer, or gave a wrong/unsupported value -> FAILURE.

QUESTION: {ques}
REFERENCE ANSWER (ground truth): {ref}
AGENT'S ANSWER: {answer}

Output ONLY one JSON object as the LAST line, nothing after it:
{{"verdict": "SUCCESS", "reason": "<one sentence>"}}
or
{{"verdict": "FAILURE", "reason": "<one sentence>"}}
"""


def agent_docker_prompt(task, cname):
    return AGENT_DOCKER_PROMPT.format(cname=cname, max_steps=MAX_TURNS, ques=task["ques"])


def claude_code_prompt(task):
    return CLAUDE_CODE_PROMPT.format(ques=task["ques"])


def grader_prompt(task, answer, ref):
    return GRADER_PROMPT.format(
        ques=task["ques"],
        ref=ref or "(none)",
        answer=answer or "(no answer produced)",
    )
