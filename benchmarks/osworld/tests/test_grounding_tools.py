"""Tests for the grounding MCP tools (benchmarks/osworld/mcp/grounding_tools.py).

Exercises the tool layer against a fake Controller: which coordinates get clicked, what the model
is told when a click changes nothing, and -- the property that matters most -- that an
unresolvable target never produces a click at a guessed coordinate.

No `mcp` import needed: the plain functions take a controller and return a string, and only
`register()` touches FastMCP (covered at the end with a stub).

  python -m benchmarks.osworld.tests.test_grounding_tools
"""
import json

from benchmarks.osworld import config
from benchmarks.osworld.mcp import grounding_tools as gt
from benchmarks.osworld.tests.test_grounding import REAL_EMPTY_ENVELOPE

NS = ('xmlns:st="https://accessibility.ubuntu.example.org/ns/state" '
      'xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"')

TREE = f"""<desktop {NS}>
  <push-button name="Bold" st:showing="true" st:visible="true" st:enabled="true"
               cp:screencoord="(200, 50)" cp:size="(24, 24)"/>
  <push-button name="Bold text" st:showing="true" st:visible="true" st:enabled="true"
               cp:screencoord="(300, 50)" cp:size="(24, 24)"/>
  <menu name="Format" st:showing="true" st:visible="true" st:enabled="true"
        cp:screencoord="(400, 10)" cp:size="(60, 20)"/>
  <entry name="Name Box" st:showing="true" st:visible="true" st:editable="true"
         cp:screencoord="(10, 80)" cp:size="(90, 20)"/>
</desktop>"""

# same desktop after the click opened a menu and moved focus
TREE_AFTER = f"""<desktop {NS}>
  <push-button name="Bold" st:showing="true" st:visible="true" st:enabled="true"
               cp:screencoord="(200, 50)" cp:size="(24, 24)"/>
  <push-button name="Bold text" st:showing="true" st:visible="true" st:enabled="true"
               cp:screencoord="(300, 50)" cp:size="(24, 24)"/>
  <menu name="Format" st:showing="true" st:visible="true" st:enabled="true"
        st:focused="true" cp:screencoord="(400, 10)" cp:size="(60, 20)"/>
  <menu-item name="Character..." st:showing="true" st:visible="true" st:enabled="true"
             cp:screencoord="(400, 40)" cp:size="(120, 20)"/>
  <entry name="Name Box" st:showing="true" st:visible="true" st:editable="true"
         cp:screencoord="(10, 80)" cp:size="(90, 20)"/>
</desktop>"""


class FakeCtrl:
    """`trees` are returned in order; the last one repeats, so a single-element list means "the
    tree never changes" -- exactly the no-effect case click_element has to detect."""

    def __init__(self, trees=(TREE,), fail=None):
        self.trees = list(trees)
        self.fail = fail
        self.clicks = []
        self.tree_calls = 0

    def a11y_tree(self):
        self.tree_calls += 1
        if self.fail == "tree":
            raise ConnectionError("controller unreachable")
        if self.fail == "empty":
            return "   "
        if self.fail == "malformed":
            return "<desktop><unclosed>"
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def pyautogui(self, code):
        if self.fail == "click":
            raise ConnectionError("run_python failed")
        self.clicks.append(code)
        return "ok"


class _Recorder:
    """Minimal stand-in for FastMCP: records what register() attaches."""

    def __init__(self):
        self.registered = {}

    def tool(self, name=None):
        def deco(fn):
            self.registered[name or fn.__name__] = fn
            return fn
        return deco


# --- find_element ------------------------------------------------------------

def test_find_element_ranks_candidates_with_coordinates():
    out = gt.find_element(FakeCtrl(), "Bold")
    first = out.splitlines()[0]
    assert "(212,62)" in first and "push-button" in first and "'Bold'" in first
    assert "Bold text" in out          # the weaker match is still offered


def test_find_element_respects_the_role_filter_and_limit():
    assert "menu" in gt.find_element(FakeCtrl(), "Format", role="menu")
    assert len(gt.find_element(FakeCtrl(), "Bold", limit=1).splitlines()) == 1


def test_find_element_tells_the_model_to_fall_back_when_nothing_resolves():
    out = gt.find_element(FakeCtrl(), "Publish to Salesforce")
    assert "no element resolved" in out and "coordinates" in out


def test_tree_failures_all_produce_an_actionable_sentence_not_a_traceback():
    for mode, expected in (("tree", "could not read the accessibility tree"),
                           ("empty", "came back empty"),
                           ("malformed", "did not parse as XML")):
        out = gt.find_element(FakeCtrl(fail=mode), "Bold")
        assert expected in out, (mode, out)
        assert "click(x, y)" in out, mode


def test_the_real_guest_envelope_is_unwrapped_before_parsing():
    """Controller.a11y_tree() returns `{"AT": "<xml>"}`, not raw XML. Without unwrapping, every
    call reported "did not parse as XML" -- wrong, and unactionable for the model."""
    enveloped = json.dumps({"AT": TREE})
    out = gt.find_element(FakeCtrl([enveloped]), "Bold")
    assert "(212,62)" in out and "did not parse" not in out


def test_a_root_only_tree_is_reported_as_the_bridge_not_reporting():
    """The ONLY case seen live: all 456 a11y_tree captures on disk come back as a self-closing
    root. The model must be told the channel is dead in this session, not handed an empty candidate
    list that reads like "this particular element is missing"."""
    out = gt.find_element(FakeCtrl([REAL_EMPTY_ENVELOPE]), "Bold")
    assert "no elements at all" in out and "AT-SPI bridge" in out and "click(x, y)" in out

    ctrl = FakeCtrl([REAL_EMPTY_ENVELOPE])
    assert "no elements at all" in gt.click_element(ctrl, "Bold")
    assert ctrl.clicks == []          # and nothing is clicked on a guessed coordinate


# --- click_element -----------------------------------------------------------

def test_click_element_clicks_the_resolved_centre():
    ctrl = FakeCtrl()
    out = gt.click_element(ctrl, "Bold")
    assert ctrl.clicks == ["import pyautogui; pyautogui.click(212, 62)"]
    assert "clicked push-button 'Bold' at (212,62)" in out


def test_click_element_rank_selects_a_lower_candidate():
    ctrl = FakeCtrl()
    gt.click_element(ctrl, "Bold", rank=2)
    assert ctrl.clicks == ["import pyautogui; pyautogui.click(312, 62)"]


def test_click_element_out_of_range_rank_lists_candidates_without_clicking():
    ctrl = FakeCtrl()
    out = gt.click_element(ctrl, "Bold", rank=99)
    assert ctrl.clicks == []
    assert "rank 99 requested" in out and "candidate(s) resolved" in out


def test_click_element_never_clicks_a_guess_when_nothing_resolves():
    """The load-bearing safety property: this function cannot see the screenshot, so a guessed
    coordinate would be strictly worse than handing the decision back to the agent."""
    ctrl = FakeCtrl()
    out = gt.click_element(ctrl, "Publish to Salesforce")
    assert ctrl.clicks == []
    assert "no element resolved" in out


def test_click_element_reports_double_and_alternate_buttons():
    ctrl = FakeCtrl()
    gt.click_element(ctrl, "Bold", double=True)
    assert "doubleClick(212, 62)" in ctrl.clicks[-1]
    gt.click_element(ctrl, "Bold", button="right")
    assert "rightClick(212, 62)" in ctrl.clicks[-1]
    gt.click_element(ctrl, "Bold", button="middle")
    assert "middleClick(212, 62)" in ctrl.clicks[-1]


def test_click_element_reports_a_failed_click_with_the_coordinates_it_tried():
    out = gt.click_element(FakeCtrl(fail="click"), "Bold")
    assert "resolved 'Bold' to (212,62)" in out and "the click failed" in out


# --- the action-level check --------------------------------------------------

def test_unchanged_tree_is_reported_as_a_no_effect_click_with_alternatives():
    """A click that changed nothing is the failure this harness exists to catch, and it is
    detectable locally -- no model call, no oracle."""
    ctrl = FakeCtrl([TREE])          # same tree before and after
    out = gt.click_element(ctrl, "Bold")
    assert "WARNING" in out and "unchanged" in out
    assert "Do not repeat it identically" in out
    assert "retry with rank=N" in out and "Bold text" in out
    assert ctrl.tree_calls == 2


def test_unchanged_tree_with_no_alternatives_says_to_use_the_screenshot():
    ctrl = FakeCtrl([TREE])
    out = gt.click_element(ctrl, "Name Box")      # only one candidate resolves
    assert "WARNING" in out
    assert "no other candidate resolved" in out and "screenshot" in out


def test_changed_tree_reports_where_focus_went():
    ctrl = FakeCtrl([TREE, TREE_AFTER])
    out = gt.click_element(ctrl, "Format")
    assert "WARNING" not in out
    assert "focus moved to menu 'Format'" in out


def test_changed_tree_without_a_focus_move_still_reports_the_change():
    no_focus_after = TREE_AFTER.replace(' st:focused="true"', "")
    out = gt.click_element(FakeCtrl([TREE, no_focus_after]), "Format")
    assert "WARNING" not in out and "the UI changed" in out


def test_verification_can_be_switched_off_and_then_costs_no_extra_round_trip():
    original = config.GROUNDING_VERIFY
    try:
        config.GROUNDING_VERIFY = False
        ctrl = FakeCtrl([TREE])
        out = gt.click_element(ctrl, "Bold")
        assert ctrl.tree_calls == 1
        assert "WARNING" not in out and "clicked push-button" in out
    finally:
        config.GROUNDING_VERIFY = original


def test_a_tree_failure_after_the_click_does_not_hide_that_the_click_happened():
    class FlakyAfter(FakeCtrl):
        def a11y_tree(self):
            self.tree_calls += 1
            if self.tree_calls > 1:
                raise ConnectionError("gone")
            return TREE

    ctrl = FlakyAfter()
    out = gt.click_element(ctrl, "Bold")
    assert ctrl.clicks == ["import pyautogui; pyautogui.click(212, 62)"]
    assert "clicked push-button 'Bold'" in out
    assert "could not re-read the tree" in out


# --- list_elements ----------------------------------------------------------

def test_list_elements_filters_by_role_and_name():
    out = gt.list_elements(FakeCtrl(), role="push-button")
    assert "Bold" in out and "Format" not in out

    out = gt.list_elements(FakeCtrl(), name_contains="name box")
    assert "Name Box" in out and "Bold" not in out

    assert "no elements matched" in gt.list_elements(FakeCtrl(), role="nonexistent")


def test_list_elements_caps_the_listing():
    out = gt.list_elements(FakeCtrl(), limit=1)
    assert "showing first 1" in out


# --- registration -----------------------------------------------------------

def test_register_attaches_exactly_the_three_tools_under_their_public_names():
    rec = _Recorder()
    gt.register(rec, FakeCtrl())
    assert set(rec.registered) == {"find_element", "click_element", "list_elements"}


def test_registered_wrappers_delegate_to_the_real_implementations():
    """Guards the scoping trap: a nested `def find_element` would shadow the module-level function
    for the whole of register(), making the delegation target unreachable."""
    rec = _Recorder()
    ctrl = FakeCtrl()
    gt.register(rec, ctrl)
    assert "(212,62)" in rec.registered["find_element"]("Bold")
    rec.registered["click_element"]("Bold")
    assert ctrl.clicks[-1] == "import pyautogui; pyautogui.click(212, 62)"
    assert "Bold" in rec.registered["list_elements"](role="push-button")


def test_registered_tools_carry_docstrings_the_model_can_act_on():
    """These docstrings are the model's only documentation for the tools -- an empty one would ship
    a tool nobody knows when to use."""
    rec = _Recorder()
    gt.register(rec, FakeCtrl())
    for name, fn in rec.registered.items():
        assert fn.__doc__ and len(fn.__doc__) > 200, name


# --- real FastMCP registration and the server gate --------------------------

def test_real_fastmcp_accepts_the_signatures_and_exposes_the_three_tools():
    """The _Recorder above never validates a schema. FastMCP derives the tool's JSON schema from
    the annotations, so a signature it cannot describe (or a missing annotation) only fails here --
    registered onto a throwaway instance, so the shared server singleton is untouched."""
    import asyncio

    from mcp.server.fastmcp import FastMCP

    m = FastMCP("grounding-test")
    gt.register(m, FakeCtrl())
    tools = {t.name: t for t in asyncio.run(m.list_tools())}
    assert set(tools) == {"find_element", "click_element", "list_elements"}

    props = tools["click_element"].inputSchema["properties"]
    assert props["description"]["type"] == "string"
    assert props["rank"]["type"] == "integer"
    assert props["double"]["type"] == "boolean"
    assert tools["click_element"].inputSchema.get("required", []) == ["description"]


def test_the_server_does_not_expose_the_grounding_tools_unless_the_flag_is_set():
    """Availability is gated in mcp/server.py on OSW_GROUNDING, which is unset in this test
    environment. The gate and prompts.GROUNDING_LINES must move together: advertising a tool the
    model then finds genuinely absent is what produced fabricated tool-call text in
    docs/finding-confabulation-under-tool-denial.md."""
    import asyncio
    import os

    from benchmarks.osworld.mcp import server

    if os.environ.get("OSW_GROUNDING") == "1":
        return          # the flag really is on in this shell; nothing to assert
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not ({"find_element", "click_element", "list_elements"} & names)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    main()
