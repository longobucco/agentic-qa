"""Declared Phase 3 intervention arms for the GPT Astra runner.

An arm only ADDS to the official protocol: the upstream system prompt, the single `computer` tool,
the step budget and the evaluators stay exactly as in the replication. It always runs in its own
results tree (config.astra_system_name appends the arm name), records what it added in every
run's provenance, and is never pooled with the replication numbers. Rationale and design:
campaign_analysis/ASTRA_IMPROVEMENT_RESEARCH.md section 6 (local documentation).
"""
import hashlib

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

ADDENDA = {"e1card": E1_CARD}


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
    return {"arm": name or None,
            "arm_addendum_sha256": hashlib.sha256(extra.encode()).hexdigest() if extra else None}
