"""The grounding harness's three MCP tools: resolve a named target, click it, enumerate what's
there. Registered onto the main OSWorld server only when config.GROUNDING is on.

SHAPE OF THE INTERVENTION. `click(x, y)` is left completely untouched and still available -- this
is an ADDED channel, not a replacement. The 2026 grounding literature is explicit that
accessibility trees are noisy and incomplete on custom-rendered widgets (UGround/SeeAct-V,
arXiv:2410.05243; see docs/ideas-to-explore.md #2's corrected framing), so a design that forced
every click through the tree would trade one failure mode for another. What the agent gets here
is a structured path to try first, and a truthful answer when it does not resolve -- at which
point falling back to reading the screenshot is the correct move, and the tool says so rather than
clicking a guess.

WHY NOT A TOOL PER APPLICATION. A Calc cell is a `table-cell` whose accessible name is its
reference, a toolbar control is a `push-button` with a name: both resolve through the same generic
path, so there is no per-app code here and none is planned. That boundary is deliberate -- the
previous round of narrow content-inspection tools (mcp/readonly_server.py's
inspect_pptx_text_colors / inspect_thunderbird_prefs) worked correctly and still changed 0 of 9
outcomes, and scaling that shape to dozens of targeted tools was explicitly ruled out.

The plain functions take a Controller and return a string, so they are testable with a fake
controller and no `mcp` import; `register()` is the only part that needs FastMCP.
"""
import xml.etree.ElementTree as ET

from benchmarks.osworld import config, grounding

_BUTTON_CALL = {
    "left": "pyautogui.click({x}, {y})",
    "right": "pyautogui.rightClick({x}, {y})",
    "middle": "pyautogui.middleClick({x}, {y})",
}


_FALLBACK = "use screenshot() and click(x, y) instead"


def _tree(ctrl):
    """(xml, error_message). Every tool funnels the failure modes through here, so a model always
    gets a sentence it can act on instead of a traceback surfacing as an MCP error.

    The guest's /accessibility route returns a JSON envelope `{"AT": "<xml>"}`, not raw XML, so the
    payload is unwrapped before parsing -- without that step every call here reported "did not
    parse as XML", which is both wrong and unactionable. The no-node case is reported separately
    from the malformed case because in this harness it is the ONLY case: all 456 captures on disk
    come back with a self-closing root (docs/grounding-harness-plan.md §8).
    """
    try:
        raw = ctrl.a11y_tree()
    except Exception as e:
        return None, f"could not read the accessibility tree ({type(e).__name__}: {e}) -- {_FALLBACK}"
    xml = grounding.unwrap_tree(raw)
    if not xml.strip():
        return None, f"the accessibility tree came back empty -- {_FALLBACK}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return None, f"the accessibility tree did not parse as XML ({e}) -- {_FALLBACK}"
    if len(root) == 0:
        return None, ("the accessibility tree has no elements at all (the guest returned only a "
                      f"root node) -- the AT-SPI bridge is not reporting this desktop, so no "
                      f"element can be resolved by name in this session; {_FALLBACK}")
    return xml, None


def find_element(ctrl, description, role="", limit=5):
    xml, err = _tree(ctrl)
    if err:
        return err
    ranked = grounding.resolve(xml, description, role=role or None,
                               limit=max(1, min(int(limit), 20)),
                               min_score=config.GROUNDING_MIN_SCORE)
    return grounding.format_candidates(ranked, query=description)


def list_elements(ctrl, role="", name_contains="", limit=40):
    xml, err = _tree(ctrl)
    if err:
        return err
    elements = grounding.parse_elements(xml)
    return grounding.summarize_elements(
        elements, role=role or None, name_contains=name_contains or None,
        limit=max(1, min(int(limit), 200)))


def click_element(ctrl, description, role="", rank=1, double=False, button="left"):
    """Resolve, click, and report what the click did -- see the registered docstring below."""
    xml, err = _tree(ctrl)
    if err:
        return err

    ranked = grounding.resolve(xml, description, role=role or None, limit=5,
                               min_score=config.GROUNDING_MIN_SCORE)
    if not ranked:
        # Deliberately does NOT click anything. A guessed coordinate here would be strictly worse
        # than the agent's own visual estimate, because the agent can see the screenshot and this
        # function cannot.
        return grounding.format_candidates([], query=description)

    idx = max(1, int(rank)) - 1
    if idx >= len(ranked):
        return (f"rank {rank} requested but only {len(ranked)} candidate(s) resolved for "
                f"{description!r}:\n{grounding.format_candidates(ranked, query=description)}")

    score, why, el = ranked[idx]
    x, y = el.center
    before_digest = grounding.tree_digest(xml)
    before_focus = grounding.focused(xml)

    call = _BUTTON_CALL.get(button, _BUTTON_CALL["left"])
    if double and button == "left":
        call = "pyautogui.doubleClick({x}, {y})"
    try:
        ctrl.pyautogui(f"import pyautogui; {call.format(x=x, y=y)}")
    except Exception as e:
        return f"resolved {description!r} to ({x},{y}) but the click failed ({type(e).__name__}: {e})"

    head = (f"clicked {el.role}{' ' + repr(el.name) if el.name else ''} at ({x},{y}) "
            f"[rank {idx + 1}/{len(ranked)}, score {score:.2f}, matched {why}]")
    if not config.GROUNDING_VERIFY:
        return head

    after_xml, after_err = _tree(ctrl)
    if after_err:
        return f"{head}\n(could not re-read the tree to confirm the effect: {after_err})"

    notes = []
    if grounding.tree_digest(after_xml) == before_digest:
        # The whole point of the action-level check: a click that changed nothing is the failure
        # this harness exists to catch, and it is catchable locally -- no model call, no oracle.
        notes.append("WARNING: the accessibility tree is unchanged -- this click had no visible "
                     "effect. Do not repeat it identically.")
        if len(ranked) > idx + 1:
            notes.append("other candidates for the same description (retry with rank=N):\n"
                         + grounding.format_candidates(ranked[idx + 1:], query=description))
        else:
            notes.append("no other candidate resolved; take a screenshot and click coordinates "
                         "directly, or the target may need a different description.")
    else:
        after_focus = grounding.focused(after_xml)
        if after_focus is not None and (before_focus is None
                                        or after_focus.center != before_focus.center):
            notes.append(f"focus moved to {after_focus.role}"
                         f"{' ' + repr(after_focus.name) if after_focus.name else ''}")
        else:
            notes.append("the UI changed (focus unchanged -- a menu, dialog or selection state "
                         "likely opened or moved)")
    return "\n".join([head, *notes])


def register(mcp, ctrl):
    """Attach the three tools to a FastMCP instance. Docstrings are the model's only
    documentation for them, so they state when to reach for each one and what a failure means."""
    # The wrappers carry distinct local names with an explicit tool `name=`: a nested
    # `def find_element` would make that identifier local to register() for the whole scope, so
    # the module-level function it is supposed to delegate to would be unreachable.
    @mcp.tool(name="find_element")
    def _tool_find_element(description: str, role: str = "", limit: int = 5) -> str:
        """Resolve a described UI element to screen coordinates via the accessibility tree,
        WITHOUT clicking. Returns up to `limit` ranked candidates, each with its centre point,
        role, name, and why it ranked there.

        Use this when you know what you want to interact with but would otherwise be estimating
        its position from the screenshot -- it is the difference between clicking the Bold button
        and clicking two pixels off its edge. `description` is matched against each element's
        accessible name, text, description and value, so natural wording works ("Bold", "Save
        As", "cell D7", "Toggle bold typeface"). `role` optionally restricts the search to one
        kind of element (e.g. "push-button", "menu-item", "table-cell", "entry").

        If nothing resolves, the tree genuinely does not describe that element (common for
        custom-rendered canvases and image editors): read the screenshot and use click(x, y).
        """
        return find_element(ctrl, description, role=role, limit=limit)

    @mcp.tool(name="click_element")
    def _tool_click_element(description: str, role: str = "", rank: int = 1,
                            double: bool = False, button: str = "left") -> str:
        """Resolve a described UI element and click its centre, then report whether the click
        actually changed anything.

        Prefer this over click(x, y) whenever the target has a name in the interface. `rank`
        picks among the candidates find_element would list (1 = best); `double` double-clicks;
        `button` may be "left", "right" or "middle".

        The reply is the part that matters: if the accessibility tree is unchanged after the
        click, you are told so explicitly along with the remaining candidates -- repeating the
        same click will not help, so either retry with rank=2, describe the target differently,
        or fall back to screenshot() plus click(x, y). If nothing resolved at all, nothing is
        clicked (a guessed coordinate would be worse than your own read of the screenshot).
        """
        return click_element(ctrl, description, role=role, rank=rank, double=double,
                             button=button)

    @mcp.tool(name="list_elements")
    def _tool_list_elements(role: str = "", name_contains: str = "", limit: int = 40) -> str:
        """List the interactive elements currently on screen -- one compact line each with
        coordinates, role and name.

        Use this when you do not yet know what a control is called, instead of scanning the
        screenshot for something clickable: it is the same information a11y_tree() returns, but
        filtered to what can be acted on and short enough to read. `role` and `name_contains`
        narrow the list (e.g. role="menu-item", or name_contains="export").
        """
        return list_elements(ctrl, role=role, name_contains=name_contains, limit=limit)

    return (_tool_find_element, _tool_click_element, _tool_list_elements)
