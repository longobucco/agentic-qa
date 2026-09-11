"""Unit tests for the grounding resolver (benchmarks/osworld/grounding.py).

The fixture below is written in the REAL tree format -- namespace URIs, `cp:screencoord`/`cp:size`
string pairs and `st:*="true"` state flags all taken from the installed
desktop_env/evaluators/metrics/general.py::_accessibility_ns_map and
mm_agents/accessibility_tree_wrap/heuristic_retrieve.py::judge_node, not from a guess at what the
guest emits. A resolver tested against an invented format would pass here and resolve nothing
live, which is the one failure mode these tests exist to rule out.

No pytest (same convention as the other suites here):
  python -m benchmarks.osworld.tests.test_grounding
"""
import xml.etree.ElementTree as ET

from benchmarks.osworld import grounding


def _raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError(f"expected {exc.__name__}")

NS = ('xmlns:st="https://accessibility.ubuntu.example.org/ns/state" '
      'xmlns:cp="https://accessibility.ubuntu.example.org/ns/component" '
      'xmlns:attr="https://accessibility.ubuntu.example.org/ns/attributes" '
      'xmlns:val="https://accessibility.ubuntu.example.org/ns/value"')

SCREEN = (1920, 1080)

# A LibreOffice-Calc-shaped desktop: a full-screen frame named after the document (the classic
# wrong pick for a name query), a toolbar with two similarly-named buttons, a menu, a focused
# cell, a text entry holding task data in val:value, and three nodes that must be dropped.
TREE = f"""<desktop {NS}>
  <application name="soffice">
    <frame name="Save" st:showing="true" st:visible="true" st:enabled="true"
           cp:screencoord="(0, 0)" cp:size="(1920, 1080)"/>
    <push-button name="Save" st:showing="true" st:visible="true" st:enabled="true"
                 cp:screencoord="(100, 50)" cp:size="(32, 32)"/>
    <push-button name="Save As..." st:showing="true" st:visible="true" st:enabled="true"
                 cp:screencoord="(140, 50)" cp:size="(32, 32)"/>
    <push-button name="Bold" attr:description="Toggle bold typeface"
                 st:showing="true" st:visible="true" st:enabled="true"
                 cp:screencoord="(200, 50)" cp:size="(24, 24)"/>
    <menu name="Format" st:showing="true" st:visible="true" st:enabled="true"
          cp:screencoord="(300, 10)" cp:size="(60, 20)"/>
    <table-cell name="D7" st:showing="true" st:visible="true" st:enabled="true"
                st:focused="true" cp:screencoord="(420, 300)" cp:size="(80, 20)"/>
    <table-cell name="D8" st:showing="true" st:visible="true" st:enabled="true"
                cp:screencoord="(420, 320)" cp:size="(80, 20)"/>
    <entry name="Name Box" val:value="D7" st:showing="true" st:visible="true"
           st:editable="true" cp:screencoord="(10, 80)" cp:size="(90, 20)"/>
    <label st:showing="true" st:visible="true" st:enabled="true"
           cp:screencoord="(600, 400)" cp:size="(120, 16)">Total revenue</label>
    <push-button name="Offscreen" st:showing="true" st:visible="true" st:enabled="true"
                 cp:screencoord="(-5, -5)" cp:size="(20, 20)"/>
    <push-button name="NoGeometry" st:showing="true" st:visible="true" st:enabled="true"/>
    <push-button name="ZeroSize" st:showing="true" st:visible="true" st:enabled="true"
                 cp:screencoord="(500, 500)" cp:size="(0, 0)"/>
    <push-button name="Disabled Export" st:showing="true" st:visible="true"
                 cp:screencoord="(700, 50)" cp:size="(40, 20)"/>
  </application>
</desktop>"""


def _by_name(elements, name, role=None):
    """Note the `role`: the fixture deliberately names both a frame and a button "Save", so a
    name-only lookup returns the frame (document order) -- the same ambiguity the resolver's
    container penalty exists to break."""
    return next(el for el in elements
                if el.name == name and (role is None or el.role == role))


# --- parsing -----------------------------------------------------------------

def test_parse_keeps_only_nodes_with_usable_geometry():
    names = {el.name for el in grounding.parse_elements(TREE)}
    assert {"Save", "Save As...", "Bold", "Format", "D7", "Name Box"} <= names
    # negative position, missing screencoord/size, and a zero-size box are all unclickable
    assert "Offscreen" not in names
    assert "NoGeometry" not in names
    assert "ZeroSize" not in names


def test_parse_reads_role_states_value_and_description():
    els = grounding.parse_elements(TREE)
    bold = _by_name(els, "Bold")
    assert bold.role == "push-button"
    assert bold.description == "Toggle bold typeface"
    assert bold.states == frozenset({"showing", "visible", "enabled"})

    name_box = _by_name(els, "Name Box")
    assert name_box.value == "D7"           # val:value, not the name
    assert "editable" in name_box.states

    cell = _by_name(els, "D7")
    assert "focused" in cell.states

    label = next(el for el in els if el.role == "label")
    assert label.text == "Total revenue"    # node .text, stripped


def test_parse_reads_description_from_the_upstream_windows_namespace_too():
    """mm_agents/agent.py line 35 assigns the *windows* attributes URI to
    `attributes_ns_ubuntu` -- an upstream bug, so a guest tree may legitimately carry either URI.
    Accepting only one would silently drop every description on half the possible inputs."""
    xml = f"""<desktop {NS} xmlns:w="https://accessibility.windows.example.org/ns/attributes">
      <push-button name="Italic" w:description="Toggle italic"
                   st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="(10, 10)" cp:size="(20, 20)"/>
    </desktop>"""
    assert grounding.parse_elements(xml)[0].description == "Toggle italic"


def test_geometry_helpers():
    el = _by_name(grounding.parse_elements(TREE), "Save", "push-button")
    assert el.center == (116, 66)
    assert el.area == 32 * 32


def test_parse_honours_screen_bounds():
    """A tree can describe a window that has since closed or moved off the display."""
    xml = f"""<desktop {NS}>
      <push-button name="Far" st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="(5000, 10)" cp:size="(20, 20)"/>
    </desktop>"""
    assert grounding.parse_elements(xml) != []
    assert grounding.parse_elements(xml, screen=SCREEN) == []


def test_malformed_coordinates_are_dropped_not_evaluated():
    """Upstream judge_node calls eval() on these attributes. They are guest-supplied strings, so
    this module parses integers out of them instead -- a malformed or hostile value must drop the
    node, never execute and never raise."""
    xml = f"""<desktop {NS}>
      <push-button name="Evil" st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="(__import__('os').system('touch /tmp/pwned'), 0)"
                   cp:size="(20, 20)"/>
      <push-button name="Garbage" st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="not a tuple" cp:size="(20, 20)"/>
    </desktop>"""
    assert grounding.parse_elements(xml) == []


def test_parse_propagates_parse_error_for_the_caller_to_translate():
    _raises(ET.ParseError, grounding.parse_elements, "<desktop><unclosed>")


def test_actionable_mirrors_upstream_judge_node():
    els = grounding.parse_elements(TREE)
    assert _by_name(els, "Save", "push-button").actionable
    # showing+visible but no enabled/editable/expandable/checkable state
    assert not _by_name(els, "Disabled Export").actionable


# --- scoring and ranking -----------------------------------------------------

def test_exact_name_outranks_a_longer_partial_match():
    """"Save" must resolve to the Save button, not to "Save As...", whose raw character overlap
    with the query is high enough that a single fuzzy ratio would rank it competitively."""
    top = grounding.resolve(TREE, "Save", role="push-button", screen=SCREEN)
    assert top[0][2].name == "Save"
    assert top[1][2].name == "Save As..."
    assert top[0][0] > top[1][0]


def test_full_screen_container_loses_to_the_real_control_with_the_same_name():
    """The frame is also named "Save". A container covering the whole screen is never the click
    target -- this is the single most likely wrong pick for any name query."""
    ranked = grounding.resolve(TREE, "Save", screen=SCREEN)
    assert ranked[0][2].role == "push-button"
    frame_rank = next(i for i, (_s, _w, el) in enumerate(ranked) if el.role == "frame")
    assert frame_rank > 0
    assert "container penalty" in ranked[frame_rank][1]


def test_role_filter_excludes_other_roles_entirely():
    ranked = grounding.resolve(TREE, "Format", role="menu", screen=SCREEN)
    assert [el.role for _s, _w, el in ranked] == ["menu"]


def test_description_and_value_are_matchable_but_rank_below_name():
    by_desc = grounding.resolve(TREE, "toggle bold typeface", screen=SCREEN)
    assert by_desc[0][2].name == "Bold"
    assert "description=" in by_desc[0][1]

    # "D7" is both a cell's name and the Name Box's value; the name match must win
    ranked = grounding.resolve(TREE, "D7", screen=SCREEN)
    assert ranked[0][2].name == "D7" and ranked[0][2].role == "table-cell"


def test_a_spreadsheet_cell_resolves_through_the_generic_path():
    """No per-app tool: a Calc cell is just a `table-cell` whose accessible name is its
    reference, so the general resolver reaches it. Keeping this general is the whole point --
    a tool per application is the sprawl this harness is meant to avoid."""
    ranked = grounding.resolve(TREE, "D8", role="table-cell", screen=SCREEN)
    assert ranked[0][2].center == (460, 330)


def test_unresolvable_query_returns_empty_so_the_caller_falls_back():
    assert grounding.resolve(TREE, "Publish to Salesforce", screen=SCREEN) == []


def test_ranking_is_total_and_therefore_stable():
    """`rank=2` has to mean the same element on two identical trees, so no two candidates may
    compare equal under the sort key."""
    ranked = grounding.resolve(TREE, "Save", screen=SCREEN, limit=99, min_score=0.0)
    keys = [(-s, el.area, el.role, el.name) for s, _w, el in ranked]
    assert len(keys) == len(set(keys))

    # Element carries no __eq__, so compare the projection the caller actually acts on -- the
    # coordinates and identity each rank resolves to, which is what must be reproducible.
    def shape(rs):
        return [(round(s, 6), el.role, el.name, el.center) for s, _w, el in rs]

    assert shape(grounding.resolve(TREE, "Save", screen=SCREEN)) == shape(
        grounding.resolve(TREE, "Save", screen=SCREEN))


def test_limit_and_min_score_are_both_applied():
    assert len(grounding.resolve(TREE, "Save", screen=SCREEN, limit=1)) == 1
    assert grounding.resolve(TREE, "Save", min_score=0.999, screen=SCREEN,
                             role="push-button")[0][2].name == "Save"
    # a prefix-only query is a real but partial match, so a near-1.0 floor must exclude it
    # ("Save As" is NOT a valid case here: trailing ellipsis is presentation, so it matches
    #  "Save As..." outright -- see _text_score's f_clean)
    assert grounding.resolve(TREE, "Sav", min_score=0.999, screen=SCREEN) == []
    assert grounding.resolve(TREE, "Sav", min_score=0.4, screen=SCREEN) != []


def test_normalize_role_accepts_the_spellings_a_caller_will_actually_use():
    for spelling in ("push-button", "push_button", "Push Button", "PUSH-BUTTON"):
        assert grounding.normalize_role(spelling) == "push-button"
    assert grounding.normalize_role(None) == ""


# --- rendering ---------------------------------------------------------------

def test_format_candidates_gives_coordinates_and_the_reason():
    out = grounding.format_candidates(
        grounding.resolve(TREE, "Bold", screen=SCREEN), query="Bold")
    assert "(212,62)" in out and "push-button" in out and "score=" in out


def test_format_candidates_tells_the_model_to_fall_back_when_nothing_resolved():
    out = grounding.format_candidates([], query="Nonexistent")
    assert "no element resolved" in out and "coordinates" in out


def test_summarize_elements_filters_and_reports_the_total():
    els = grounding.parse_elements(TREE)
    buttons = grounding.summarize_elements(els, role="push-button")
    assert "Save" in buttons and "Format" not in buttons

    named = grounding.summarize_elements(els, name_contains="save")
    assert "Save As..." in named and "Bold" not in named

    capped = grounding.summarize_elements(els, limit=2)
    assert "showing first 2" in capped and len(capped.splitlines()) == 3

    assert grounding.summarize_elements(els, role="nonexistent-role") == "no elements matched"


def test_summarize_elements_can_include_non_actionable_nodes():
    els = grounding.parse_elements(TREE)
    assert "Disabled Export" not in grounding.summarize_elements(els)
    assert "Disabled Export" in grounding.summarize_elements(els, actionable_only=False)


# --- post-action signals -----------------------------------------------------

def test_focused_identifies_the_element_holding_focus():
    assert grounding.focused(TREE).name == "D7"
    xml = f'<desktop {NS}><push-button name="A" cp:screencoord="(1, 1)" cp:size="(2, 2)"/></desktop>'
    assert grounding.focused(xml) is None


def test_tree_digest_is_order_independent_but_state_sensitive():
    a = f"""<desktop {NS}>
      <check-box name="Bold" st:showing="true" st:visible="true" st:checkable="true"
                 cp:screencoord="(10, 10)" cp:size="(20, 20)"/>
      <push-button name="OK" st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="(50, 10)" cp:size="(20, 20)"/>
    </desktop>"""
    reordered = f"""<desktop {NS}>
      <push-button name="OK" st:showing="true" st:visible="true" st:enabled="true"
                   cp:screencoord="(50, 10)" cp:size="(20, 20)"/>
      <check-box name="Bold" st:showing="true" st:visible="true" st:checkable="true"
                 cp:screencoord="(10, 10)" cp:size="(20, 20)"/>
    </desktop>"""
    checked = a.replace('name="Bold" st:showing', 'name="Bold" st:checked="true" st:showing')
    moved = a.replace('cp:screencoord="(50, 10)"', 'cp:screencoord="(50, 40)"')

    assert grounding.tree_digest(a) == grounding.tree_digest(reordered)
    assert grounding.tree_digest(a) != grounding.tree_digest(checked)
    assert grounding.tree_digest(a) != grounding.tree_digest(moved)


def test_tree_digest_returns_empty_on_unparseable_input():
    """The digest answers "did anything change"; a momentarily malformed tree must not raise into
    the middle of an action."""
    assert grounding.tree_digest("<not xml") == ""


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
