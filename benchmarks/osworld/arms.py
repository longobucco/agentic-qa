"""Declared Phase 3 intervention arms for the GPT Astra runner.

An arm only ADDS to the official protocol: the upstream system prompt, the single `computer` tool,
the step budget and the evaluators stay exactly as in the replication. It always runs in its own
results tree (config.astra_system_name appends the arm name), records what it added in every
run's provenance, and is never pooled with the replication numbers. Rationale and design:
campaign_analysis/ASTRA_IMPROVEMENT_RESEARCH.md section 6 (local documentation).
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

ADDENDA = {"e1card": E1_CARD, "e1bounce": E1_CARD}
BOUNCE_ARMS = frozenset({"e1bounce"})


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
        raise SystemExit(f"OSW_ASTRA_ARM={name!r}: unknown arm (known: {', '.join(sorted(ADDENDA))})")
    return ADDENDA[name]


def system_prompt(official, name):
    """The base instructions for a run: the official prompt, plus the arm's addendum if any."""
    extra = addendum(name)
    return f"{official}\n\n{extra}" if extra else official


def provenance(name):
    """What a run records about its arm: the name and a hash of the exact text it added."""
    extra = addendum(name)
    rec = {"arm": name or None,
           "arm_addendum_sha256": hashlib.sha256(extra.encode()).hexdigest() if extra else None}
    if bounces(name):
        rec["arm_bounce_message_sha256"] = hashlib.sha256(BOUNCE_MESSAGE.encode()).hexdigest()
    return rec
