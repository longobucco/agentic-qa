"""Declared Phase 3 intervention arms, for both runners (OSW_ARM: Claude, OSW_ASTRA_ARM: Astra).

An arm only ADDS to the official protocol: the upstream system prompt, the single `computer` tool,
the step budget and the evaluators stay exactly as in the replication. It always runs in its own
results tree (config appends the arm name to the tree name), records what it added in every
run's provenance, and is never pooled with the replication numbers. The groups each arm runs
on are declared in phase3.py.
"""
import hashlib
import re

# E1 (capability card): generic knowledge about this guest, never keyed to particular tasks. It
# names the fallback the agent never tried on the settings tasks (a terminal and gsettings/dconf
# or config files) and asks for a direct probe before an infeasibility verdict.
E1_CARD = (
    "Note on this environment: the desktop session is minimal (an Openbox window manager, no "
    "GNOME Shell, no panel and no Settings application). When a graphical settings or "
    "configuration application is missing, the underlying settings usually remain readable and "
    "writable from a terminal: desktop settings through gsettings or dconf, applications through "
    "their own configuration files. Before concluding that a task is infeasible, check directly "
    "whether the capability it needs is really missing, for example by querying the relevant "
    "setting from a terminal, and answer [INFEASIBLE] only if that check shows the task cannot "
    "be done. Prefer the graphical interface whenever it is available."
)

# E1 bounce: the same card, plus one bounce of an [INFEASIBLE] answer given without a direct probe
# of the missing capability (the research report's "INFEASIBLE needs evidence" rule). The
# resumed session continues the same step budget; the episode's answer is read from the part
# after the bounce, because the rejected [INFEASIBLE] would otherwise end it under the upstream
# rule ("[INFEASIBLE] anywhere -> FAIL").
BOUNCE_MESSAGE = (
    "Your [INFEASIBLE] answer is not accepted yet: the session shows no direct check that the "
    "capability the task needs is really missing. Check it now, for example by querying the "
    "relevant setting, file or program from a terminal. If the check shows the task can be done, "
    "do it; if it confirms the task is impossible, answer [INFEASIBLE] again and name the "
    "evidence."
)

# Shell commands that count as a direct probe of a capability, when typed by the agent.
_PROBE = re.compile(r"\b(gsettings|dconf|which|command -v|dpkg|apt-cache|apt list|xdg-mime|"
                    r"pactl|ls |cat |grep |find |--help|--version|systemctl|snap list|flatpak)\b")

# E1b (capability + evaluator-awareness card): E1 plus generic rules about how results are
# checked. Distilled from the knowledge banks of the leading open harnesses (Pointer's
# knowledge.py, Intelligence-Indeed's generic skills; research report of 21-22/10), never from the
# evaluated tasks or their checkers; the official [INFEASIBLE] rule is restated, not relaxed.
E1B_CARD = E1_CARD + (
    "\n\nHow results are checked: the outcome is read from the real state of the system after "
    "you finish (files on disk, application settings and preference files, command output), not "
    "from what the screen shows. Before you answer, make sure the result is persisted: save the "
    "document in the requested file and format, and keep any setting live in the application it "
    "belongs to. Use the application's real feature for what is asked (a real pivot table, a real "
    "hidden row, a slide-master property, the application's own export), not something that only "
    "looks the same. Do not add content the task did not ask for, and choose exactly the colour, "
    "value or name that is requested. If you change an application's file from a terminal, close "
    "the application first and reopen the file afterwards, so the application does not overwrite "
    "your change. If the task is genuinely impossible, answer [INFEASIBLE] as usual."
)

# E1 card v2: E1 found the card turns correct refusals into false DONEs on infeasible tasks (both
# models, X5): the agent works around a confirmed gap. Same card, the infeasibility rule closed.
E1C2_CARD = E1_CARD.replace(
    "and answer [INFEASIBLE] only if that check shows the task cannot be done.",
    "and answer [INFEASIBLE] only if that check shows the task cannot be done. If it confirms the "
    "capability is missing, answer [INFEASIBLE]: do not work around it by installing or "
    "downloading software, or by building a substitute for what the task asks.")
assert E1C2_CARD != E1_CARD

# Declared code arm: the E1 card v2 plus a second tool, run_command, served by the same MCP server
# and executed inside the guest (never on the harness host). Each call is one step of the budget.
CODE_CARD = E1C2_CARD + (
    "\n\nYou also have a second tool, run_command, which runs one bash command inside this Ubuntu "
    "desktop and returns its exit code, standard output and standard error. Use it where a "
    "terminal is the better route: reading or changing settings (gsettings, dconf, configuration "
    "files), and inspecting or editing files. Each call counts as one step of your budget, like a "
    "computer action. A command runs for at most `timeout` seconds (default 60, at most 110). If "
    "you change an application's file with a command, close the application first and reopen "
    "the file afterwards, so the application does not overwrite your change. After changing "
    "something, check the result before you answer."
)

ADDENDA = {"e1card": E1_CARD, "e1bounce": E1_CARD, "e1b": E1B_CARD, "e1card2": E1C2_CARD,
           "code": CODE_CARD}
BOUNCE_ARMS = frozenset({"e1bounce"})
# MCP tools an arm adds next to `computer` (mcp/server.py registers them only for that arm).
ARM_TOOLS = {"code": ("run_command",)}


def bounces(name):
    """Whether arm `name` bounces an unsupported [INFEASIBLE] answer once."""
    return name in BOUNCE_ARMS


def has_probe(typed_texts):
    """True when any text the agent typed into the desktop contains a capability probe."""
    return any(_PROBE.search(t or "") for t in typed_texts)


def addendum(name):
    """The text arm `name` appends to the official system prompt ('' for the official protocol).
    An unknown arm name is refused: a typo must never silently run the plain protocol."""
    if not name:
        return ""
    if name not in ADDENDA:
        raise SystemExit(f"arm {name!r}: unknown arm (known: {', '.join(sorted(ADDENDA))})")
    return ADDENDA[name]


def tools(name):
    """The MCP tools arm `name` adds next to `computer` (() for the official protocol)."""
    addendum(name)
    return ARM_TOOLS.get(name, ())


def system_prompt(official, name):
    """The base instructions for a run: the official prompt, plus the arm's addendum if any."""
    extra = addendum(name)
    return f"{official}\n\n{extra}" if extra else official


def provenance(name):
    """What a run records about its arm: the name and a hash of the exact text it added."""
    extra = addendum(name)
    rec = {"arm": name or None,
           "arm_addendum_sha256": hashlib.sha256(extra.encode()).hexdigest() if extra else None}
    if tools(name):
        rec["arm_tools"] = list(tools(name))
    if bounces(name):
        rec["arm_bounce_message_sha256"] = hashlib.sha256(BOUNCE_MESSAGE.encode()).hexdigest()
    return rec
