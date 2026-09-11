"""Target resolution from the accessibility tree -- the pure-logic half of the grounding harness.

READ THIS FIRST: THE CHANNEL IS DEAD IN THIS HARNESS. All 456 real a11y_tree captures on disk --
every results tree, all 9 apps, both the mixed and the pinned Sonnet-5 campaign -- return an EMPTY
tree: `{"AT": "<desktop-frame xmlns:.../>"}`, a self-closing root with zero child nodes. Not once
populated. The guest image installs `at-spi2-core` and `python3-pyatspi`
(docker/Dockerfile.osworld), so the dependency is present, but no AT-SPI bridge is actually
producing a tree at runtime, and nothing in this module can resolve anything until that is fixed
(accessibility bus running, toolkit bridges enabled for GTK/Qt apps).

That finding also corrects this module's own original motivation. a11y_tree's 0.8% share of tool
calls is NOT an underused structured channel the agent neglects in favour of pixels: the agent
tried it 456 times, got nothing back, and stopped. Everything here is therefore written against a
channel that must be repaired before it can be evaluated -- see docs/grounding-harness-plan.md §5
and §8. It is kept because the code is correct and tested, not because the arm is ready to run.

WHY IT WAS BUILT. Every G5 arm so far acted AFTER the action: self-verify (#1, null), independent
verifier (#9), majority vote (#12), in-loop verify (#15, null), Verify-Replan (gate failed at
57.1% precision), offline bBoN selection (#16, below chance). On the pinned Sonnet-5 tree
(agent_computer_sonnet5, 833 agent-scored runs, 851 transcripts, 56.8% pass rate) a sweep ruled
out every other structural explanation for the 37.5% of tasks that never pass:

  - turn budget is not binding -- median 17 turns in BOTH the always-pass and always-fail
    buckets, 0.0% of runs reach the 150-turn cap;
  - observation capability is not missing -- the agent already reopens and parses its own output
    files in 73.2% of always-fail runs that use run_python;
  - the full "derive the expected value, then compare" loop is already present at 10.0% in
    always-fail vs 12.9% in always-pass -- no enrichment, so forcing it has no observational
    support;
  - requirement complexity does not discriminate -- 1.16 vs 1.11 conjunctive oracle checks,
    3.86 vs 3.77 instruction clauses.

Click targeting looked like the one feature that separated the buckets, but that measurement came
from `agent_computer`, which is genuinely mixed-model (326 of 982 runs served by claude-sonnet-5,
296 by claude-sonnet-4-6, 5 by claude-opus-4-8). There the gap is real: 11.8% vs 8.7% re-clicks,
z = 2.74. On the pinned Sonnet-5 tree it vanishes and reverses -- 3.4% always-fail vs 4.3%
always-pass, z = -1.37 -- so targeting churn reads as a capability signature of the weaker models
in the mixed tree, not as the mechanism behind Sonnet 5's residual failures. One app survives
per-app (libreoffice_calc, 6.7% vs 1.1%, z = 4.65, which passes Bonferroni over the 10 apps), and
even that is unreachable while the tree is empty.

WHAT IT DOES. It moves target resolution EARLIER, to before the wrong state exists: the agent
names what it wants ("the Bold button", "cell D7") and this resolves the name to coordinates
through the structured channel, falling back to the model's own visual estimate whenever nothing
resolves -- which, today, is always. That is the Mixture-of-Grounding shape the published OSWorld
gains come from (Agent S2, arXiv:2504.00906; UGround/SeeAct-V, arXiv:2410.05243), not an
a11y-tree-first design: the 2026 grounding literature is explicit that a11y trees are noisy and
incomplete on custom-rendered widgets (docs/ideas-to-explore.md #2's corrected framing), which is
why `click(x, y)` stays available and unmodified and this is strictly an added channel.

SCOPE. Pure functions over an XML string: no network, no MCP, no Controller. Everything here is
unit-testable against a fixture, which is why the tree-format constants below are taken from
upstream rather than guessed -- see _UBUNTU_NS.
"""
import difflib
import hashlib
import json
import re
import xml.etree.ElementTree as ET

# Verified against the installed desktop_env/mm_agents, not inferred from sample output:
# desktop_env/evaluators/metrics/general.py::_accessibility_ns_map and
# mm_agents/accessibility_tree_wrap/heuristic_retrieve.py's state/component/value constants.
_UBUNTU_NS = {
    "st": "https://accessibility.ubuntu.example.org/ns/state",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
    "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
    "val": "https://accessibility.ubuntu.example.org/ns/value",
}
# mm_agents/agent.py line 35 sets `attributes_ns_ubuntu` to the *windows* attributes URL -- an
# upstream copy-paste bug, not a platform quirk (general.py's own ns map uses the ubuntu URL for
# ubuntu). A guest's tree could carry either, so both are accepted when reading `class` and
# `description`; picking one would silently drop those fields on half the possible inputs.
_ATTR_NS_ALT = "https://accessibility.windows.example.org/ns/attributes"

_COORD_RE = re.compile(r"-?\d+")

# Roles a click is normally aimed at. Used only as a small ranking bonus, never as a filter: a
# hard role whitelist is how a resolver silently fails on a custom-rendered widget, the exact
# a11y-tree weakness the grounding literature warns about.
_INTERACTIVE_ROLES = frozenset({
    "push-button", "toggle-button", "radio-button", "check-box", "menu-item",
    "check-menu-item", "radio-menu-item", "menu", "combo-box", "entry", "text",
    "password-text", "spin-button", "slider", "link", "table-cell", "list-item",
    "tree-item", "page-tab", "icon", "table-column-header", "table-row-header",
})

# Mirrors mm_agents/accessibility_tree_wrap/heuristic_retrieve.py::judge_node's role test, so
# `actionable` here means what OSWorld's own agents mean by it.
_KEEP_SUFFIXES = ("item", "button", "heading", "label", "scrollbar", "searchbox",
                  "textbox", "link", "tabelement", "textfield", "textarea", "menu")
_KEEP_EXACT = frozenset({
    "alert", "canvas", "check-box", "combo-box", "entry", "icon", "image", "paragraph",
    "scroll-bar", "section", "slider", "static", "table-cell", "terminal", "text",
    "netuiribbontab", "start", "trayclockwclass", "traydummysearchcontrol", "uiimage",
    "uiproperty", "uiribboncommandbar",
})


def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _pair(raw):
    """Parse the tree's "(x, y)" / "(w, h)" attribute form. Upstream uses eval() on these; this
    reads the two integers instead -- the string comes from the guest, and an eval() on guest
    data is an arbitrary-code path for no benefit."""
    if not raw:
        return None
    nums = _COORD_RE.findall(raw)
    if len(nums) < 2:
        return None
    return int(nums[0]), int(nums[1])


def normalize_role(role):
    """`push_button`, `Push Button`, `pushbutton` -> `push-button`, so a caller never has to
    match the tree's exact spelling of a role."""
    if not role:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", str(role).strip().lower()).strip("-")


class Element:
    """One resolved accessibility node: role, the strings it can be matched on, and a click point.

    `states` holds only the state flags the tree reports as "true", so membership tests read as
    the question being asked (`"focused" in el.states`).
    """

    __slots__ = ("role", "name", "text", "description", "value", "css_class",
                 "x", "y", "w", "h", "states", "path")

    def __init__(self, *, role, name="", text="", description="", value="", css_class="",
                 x=0, y=0, w=0, h=0, states=frozenset(), path=""):
        self.role, self.name, self.text = role, name, text
        self.description, self.value, self.css_class = description, value, css_class
        self.x, self.y, self.w, self.h = x, y, w, h
        self.states, self.path = frozenset(states), path

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def area(self):
        return max(self.w, 0) * max(self.h, 0)

    @property
    def fields(self):
        """Match targets, most to least authoritative -- `name` is the accessible name a person
        would call the element, `value` is last because a text field's content often repeats task
        data and would otherwise out-match the field's own label."""
        return (("name", self.name), ("text", self.text),
                ("description", self.description), ("value", self.value))

    @property
    def actionable(self):
        """Upstream judge_node's notion of a relevant node, minus its coordinate check (already
        enforced at parse time). Kept faithful on purpose: `list_elements` showing a different
        set than OSWorld's own agents consider relevant would make cross-harness comparison of a
        grounding result meaningless."""
        role = self.role
        role_ok = (role.startswith("document") or role.endswith(_KEEP_SUFFIXES)
                   or role in _KEEP_EXACT)
        visible = "showing" in self.states and "visible" in self.states
        usable = bool(self.states & {"enabled", "editable", "expandable", "checkable"})
        named = bool(self.name or self.text)
        return role_ok and visible and usable and named

    def describe(self):
        bits = [self.role]
        if self.name:
            bits.append(f"name={self.name!r}")
        if self.text and self.text != self.name:
            bits.append(f"text={self.text[:60]!r}")
        if self.value:
            bits.append(f"value={self.value[:40]!r}")
        cx, cy = self.center
        bits.append(f"at=({cx},{cy}) box={self.w}x{self.h}")
        return " ".join(bits)

    def __repr__(self):
        return f"<Element {self.describe()}>"


def unwrap_tree(raw):
    """The guest's /accessibility route does NOT return raw XML: it returns a JSON object
    `{"AT": "<desktop-frame .../>"}`, so Controller.a11y_tree() hands back that envelope verbatim.

    Found by reading the 456 real a11y_tree captures on disk rather than by inspection -- every
    one of them is the envelope form, and parsing it as XML fails outright. Accepts either shape
    (and tolerates the extra `{"result": ...}` layer the MCP transport adds when a tool result is
    logged) so the resolver works against the controller, against a transcript capture, and
    against a plain XML fixture.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text.startswith("{"):
        return text
    for _ in range(2):          # at most {"result": "{\"AT\": ...}"}
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return text
        if not isinstance(obj, dict):
            return text
        nxt = obj.get("AT") if "AT" in obj else obj.get("result")
        if not isinstance(nxt, str):
            return text
        text = nxt.strip()
        if text.startswith("<"):
            return text
    return text


def parse_elements(xml, *, screen=None):
    """Every node carrying usable screen geometry, in document order.

    Nodes without a non-negative position and a positive size are dropped: upstream does the same,
    and a node with no box is not a click target. `screen`, when given as (w, h), additionally
    drops boxes that start outside it -- a stale tree can describe a window that has since closed.

    Raises ET.ParseError on malformed XML; callers at the MCP boundary turn that into a message
    rather than letting it escape (see mcp/grounding_tools.py).
    """
    root = ET.fromstring(unwrap_tree(xml))
    st, cp, val = _UBUNTU_NS["st"], _UBUNTU_NS["cp"], _UBUNTU_NS["val"]
    out = []
    for node in root.iter():
        pos = _pair(node.get(f"{{{cp}}}screencoord"))
        size = _pair(node.get(f"{{{cp}}}size"))
        if not pos or not size:
            continue
        x, y = pos
        w, h = size
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            continue
        if screen and (x >= screen[0] or y >= screen[1]):
            continue
        states = {_localname(k) for k, v in node.attrib.items()
                  if k.startswith(f"{{{st}}}") and v == "true"}
        desc = (node.get(f"{{{_UBUNTU_NS['attr']}}}description")
                or node.get(f"{{{_ATTR_NS_ALT}}}description") or "")
        css = (node.get(f"{{{_UBUNTU_NS['attr']}}}class")
               or node.get(f"{{{_ATTR_NS_ALT}}}class") or "")
        out.append(Element(
            role=_localname(node.tag), name=node.get("name") or "",
            text=(node.text or "").strip(), description=desc,
            value=node.get(f"{{{val}}}value") or "", css_class=css,
            x=x, y=y, w=w, h=h, states=states,
        ))
    return out


def _text_score(query, field):
    """0..1 similarity between a query and one field, deliberately tiered rather than a single
    fuzzy ratio: an exact name match must always outrank a coincidental high character overlap
    ("Save" vs "Save As..." scores 0.75 on substring, not 0.89 on SequenceMatcher, so the plain
    "Save" item still wins)."""
    if not field:
        return 0.0
    q, f = query.strip().lower(), field.strip().lower()
    if not q or not f:
        return 0.0
    if q == f:
        return 1.0
    # trailing ellipses / accelerators / mnemonics are presentation, not identity
    f_clean = re.sub(r"[.…]+$", "", f).replace("_", "").strip()
    if q == f_clean:
        return 0.95
    if f_clean.startswith(q) or q.startswith(f_clean):
        return 0.85
    if q in f_clean or f_clean in q:
        return 0.75
    q_tokens, f_tokens = set(q.split()), set(f_clean.split())
    if q_tokens and f_tokens:
        overlap = len(q_tokens & f_tokens) / len(q_tokens | f_tokens)
        if overlap:
            return 0.3 + 0.4 * overlap
    return 0.6 * difflib.SequenceMatcher(None, q, f_clean).ratio()


_FIELD_WEIGHT = {"name": 1.0, "text": 0.92, "description": 0.8, "value": 0.7}


def score_element(el, query, *, role=None, screen_area=None):
    """(score, why) for one element against one query. Pure and deterministic -- the ranking is
    explained back to the model in `why` so a wrong pick is debuggable from the transcript
    instead of being an opaque number."""
    if role:
        want = normalize_role(role)
        if want and want not in el.role:
            return 0.0, f"role {el.role!r} does not match {want!r}"

    best, why = 0.0, "no field matched"
    for field_name, field in el.fields:
        s = _text_score(query, field) * _FIELD_WEIGHT[field_name]
        if s > best:
            best, why = s, f"{field_name}={field[:50]!r} ~ {query!r}"
    if best <= 0:
        return 0.0, why

    score = best
    bonuses = []
    if el.role in _INTERACTIVE_ROLES:
        score += 0.06
        bonuses.append("interactive role")
    if el.actionable:
        score += 0.04
        bonuses.append("actionable")
    if "focused" in el.states:
        score -= 0.02          # already focused: usually not what a click is being aimed at

    # A container whose name happens to equal the query (a frame titled after the document, a
    # panel wrapping the real control) is the classic wrong pick. Penalise by how much of the
    # screen the box covers, so the specific widget inside wins on an otherwise equal match.
    if screen_area and el.area > 0:
        share = el.area / screen_area
        if share > 0.25:
            score *= 0.55
            bonuses.append(f"container penalty ({100*share:.0f}% of screen)")
        elif share > 0.08:
            score *= 0.85
            bonuses.append(f"large-box penalty ({100*share:.0f}% of screen)")

    score = max(0.0, min(score, 1.0))
    if bonuses:
        why = f"{why} [{', '.join(bonuses)}]"
    return score, why


def tree_health(raw):
    """Is the accessibility channel actually reporting? -> {"nodes", "elements", "ok", "reason"}.

    `nodes` counts child elements of the root, `elements` those with usable geometry. `ok` is
    False for the three distinguishable failures -- unreachable/blank, unparseable, and the
    root-only tree that this harness produced on all 456 captures before the AT-SPI bus was added
    to docker/start.sh. Recorded per run so the repair is verified by data rather than asserted:
    an apt-get install is not validation (see the Dockerfile's own note).
    """
    xml = unwrap_tree(raw)
    if not xml.strip():
        return {"nodes": 0, "elements": 0, "ok": False, "reason": "empty response"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return {"nodes": 0, "elements": 0, "ok": False, "reason": f"unparseable: {e}"}
    nodes = sum(1 for _ in root.iter()) - 1
    if nodes <= 0:
        return {"nodes": 0, "elements": 0, "ok": False,
                "reason": "root node only -- the AT-SPI bridge is not reporting this desktop"}
    elements = len(parse_elements(xml))
    return {"nodes": nodes, "elements": elements, "ok": elements > 0,
            "reason": None if elements else
                      f"{nodes} node(s) but none with usable geometry"}


def screen_area_hint(elements):
    """Usable screen area inferred from the tree's own outermost box.

    The container penalty needs to know what "most of the screen" means, and asking the guest for
    the display size would be an extra round trip on every resolve. The root frame/desktop node is
    already the largest box in the tree, so its extent is the answer -- and it stays correct on a
    non-standard resolution, where a hardcoded 1920x1080 would not.
    """
    if not elements:
        return None
    return max((el.x + el.w) * (el.y + el.h) for el in elements) or None


def resolve(xml, query, *, role=None, limit=5, min_score=0.45, screen=None):
    """Rank elements against `query`. Returns [(score, why, Element)], best first, at most `limit`.

    An empty result means the structured channel could not resolve the target -- the caller's
    correct move is then the visual estimate, not a guess from this module.
    """
    elements = parse_elements(xml, screen=screen)
    screen_area = (screen[0] * screen[1]) if screen else screen_area_hint(elements)
    scored = []
    for el in elements:
        s, why = score_element(el, query, role=role, screen_area=screen_area)
        if s >= min_score:
            scored.append((s, why, el))
    # area ascending as the tiebreak: between two equally-matching boxes the smaller one is the
    # more specific target. Sorting must be total, so the role/name string is the final key --
    # Element is not orderable and an unstable sort would make `rank=2` mean different things on
    # two identical trees.
    scored.sort(key=lambda t: (-t[0], t[2].area, t[2].role, t[2].name))
    return scored[:limit]


def format_candidates(scored, *, query=""):
    """Compact ranked listing for a tool result. One line per candidate: the model needs the
    coordinates, the identity, and why it ranked there -- not an XML subtree."""
    if not scored:
        return (f"no element resolved for {query!r} -- fall back to reading the screenshot and "
                f"clicking coordinates directly")
    lines = []
    for i, (score, why, el) in enumerate(scored, 1):
        cx, cy = el.center
        lines.append(f"{i}. ({cx},{cy}) score={score:.2f} {el.role}"
                     f"{' ' + repr(el.name) if el.name else ''} box={el.w}x{el.h} -- {why}")
    return "\n".join(lines)


def summarize_elements(elements, *, role=None, name_contains=None, limit=40,
                       actionable_only=True):
    """Compact enumeration of what is actually on screen, for when the agent does not yet know
    what to name. This is the affordance a11y_tree() never provided: the raw dump is tens of
    thousands of characters, so the channel went unused (0.9% of tool calls) while the agent
    guessed coordinates from pixels instead."""
    want_role = normalize_role(role) if role else ""
    needle = (name_contains or "").strip().lower()
    rows, shown = [], 0
    total = 0
    for el in elements:
        if actionable_only and not el.actionable:
            continue
        if want_role and want_role not in el.role:
            continue
        if needle and needle not in " ".join(
                v for _k, v in el.fields if v).lower():
            continue
        total += 1
        if shown < limit:
            cx, cy = el.center
            rows.append(f"({cx},{cy}) {el.role}"
                        f"{' ' + repr(el.name) if el.name else ''}"
                        f"{' text=' + repr(el.text[:40]) if el.text and el.text != el.name else ''}")
            shown += 1
    if not rows:
        return "no elements matched"
    head = f"{total} element(s) matched" + (f", showing first {shown}" if total > shown else "")
    return head + ":\n" + "\n".join(rows)


def focused(xml):
    """The element carrying the `focused` state, or None. After a click that was supposed to
    land on a control, this answers "did my target actually take focus" locally -- no model
    call, no oracle."""
    for el in parse_elements(xml):
        if "focused" in el.states:
            return el
    return None


def tree_digest(xml, *, actionable_only=True):
    """Stable hash of the set of on-screen elements, for "did the UI change at all".

    Deliberately excludes coordinates of non-actionable nodes and any ordering beyond the sorted
    identity set, so an unrelated repaint or a caret blink does not read as a state change -- the
    question being asked is whether the action did anything, not whether pixels moved.
    """
    try:
        elements = parse_elements(xml)
    except ET.ParseError:
        return ""
    keys = sorted(
        f"{el.role}|{el.name}|{el.text[:40]}|{el.x},{el.y},{el.w},{el.h}"
        f"|{'.'.join(sorted(el.states & {'checked', 'selected', 'expanded', 'focused'}))}"
        for el in elements if el.actionable or not actionable_only
    )
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]
