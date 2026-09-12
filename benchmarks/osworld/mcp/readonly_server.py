"""OSWorld read-only MCP server: the Verify-Replan Auditor's ONLY view of the desktop
(docs/verify-replan-minimal-integration-plan.md Section 3.3, 7.1). A prompt telling the model
"don't act" is not a security boundary -- server.py's own `run_python` gate already establishes
that precedent for OSWorld (registered only when OSW_RESTRICT_RUN_PYTHON isn't set). This module
takes that one step further: it never imports or defines a single mutating tool function, so
there is nothing for the protocol's own `tools/list` to expose and nothing for a model to invoke
even if it tried -- not a filtered view of server.py, a module that structurally cannot mutate.

Exposes: `screenshot`, `a11y_tree`, `wait` (verified non-mutating by test_readonly_mcp.py's
canary-hash check, not by inspection alone) -- plus two narrow, read-only CONTENT inspection
tools added 2026-09-11 after the first Verify-Replan pilot batch found the auditor's precision
on "verified_done" capped at 35.3%/57.1% (docs/verify-replan-minimal-integration-plan.md
Section 18): most of the residual false positives were tasks whose success criterion (an exact
PPTX text color, a Thunderbird preference) simply isn't visible in a screenshot, the same
observability gap 2026 literature on GUI-agent verification (Interactive Reward Agent,
arXiv:2607.25904; MCPWorld, arXiv:2506.07672) converges on solving with "application tools" that
inspect real file/state content rather than a visual proxy.

Deliberately narrower than that literature's own approach: IRA's own tool set is 83% generic
`execute_vm_command` (arbitrary shell), which the paper never argues is safe against reading a
task's own evaluator spec or gold reference -- exactly the oracle-contamination vector already
found and documented for the ACTING agent in this project (project doc Section 6). Both tools
below take `Controller.read_file`/`execute()` calls whose command/path is written IN THIS
MODULE, never built from model-supplied code or an interpolated shell string -- the model
supplies only a plain data value (a file path to read, a substring to match), which is compared
in trusted Python here, never executed. Structurally, neither tool CAN reach the evaluator spec
or gold cache regardless of what the model asks for: both live only on the HOST filesystem
(`config.TASKS_FILE`, `_score`'s per-run `gold_dir`), never copied into the guest a normal task
run inspects.
"""
import fnmatch
import io
import os
import re
import time
import zipfile

from mcp.server.fastmcp import FastMCP, Image

from benchmarks.osworld.env.controller import Controller

mcp = FastMCP("osworld_readonly")
_ctrl = Controller(os.environ.get("OSW_CONTROLLER_URL", "http://localhost:5000"))


@mcp.tool()
def screenshot() -> Image:
    """PNG of the current screen."""
    return Image(data=_ctrl.screenshot(), format="png")


@mcp.tool()
def a11y_tree() -> str:
    """Accessibility tree, for grounding."""
    return _ctrl.a11y_tree()


@mcp.tool()
def wait(seconds: float) -> str:
    time.sleep(min(seconds, 30))
    return f"waited {seconds}s"


@mcp.tool()
def inspect_thunderbird_prefs(pattern: str) -> str:
    """Read-only: find Thunderbird's prefs.js in the current profile and return every
    `user_pref(...)` line whose text contains `pattern` (case-insensitive plain substring match
    -- never a shell pattern, never executed). Use this when a task's success depends on a
    Thunderbird setting (e.g. a filter, a theme, a display preference) that may not be visible,
    or may only be partially visible, in the Preferences UI or a single screenshot. Standard
    profile-directory convention, not task-specific information: the SAME lookup a person
    troubleshooting Thunderbird would use, unrelated to any specific task's evaluator."""
    found = _ctrl.execute(
        "find ~/.thunderbird -maxdepth 2 -iname prefs.js 2>/dev/null | head -1", shell=True,
    ) or ""
    path = found.strip()
    if not path:
        return "no prefs.js found under ~/.thunderbird"
    try:
        text = _ctrl.read_file(path).decode("utf-8", errors="replace")
    except Exception as e:
        return f"could not read {path}: {e}"
    needle = pattern.lower()
    matches = [line.strip() for line in text.splitlines() if needle in line.lower()]
    if not matches:
        return f"no user_pref line in {path} matches {pattern!r} ({len(text.splitlines())} lines checked)"
    return "\n".join(matches[:50])


@mcp.tool()
def inspect_pptx_text_colors(path: str) -> str:
    """Read-only: open a .pptx file (a zip of XML, per the OOXML format) and list every text run
    together with its actual solid-fill color as stored in the file -- not as rendered on
    screen, where anti-aliasing and color-profile differences can make a visual read unreliable
    for an exact hex comparison. `path` is a plain file path (e.g. one visible in a window title
    or already mentioned in the task instruction), read via the same Controller.read_file used
    elsewhere in this harness -- never executed as code."""
    try:
        data = _ctrl.read_file(path)
    except Exception as e:
        return f"could not read {path}: {e}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return f"{path} is not a valid .pptx (not a zip archive)"
    slide_names = sorted(n for n in zf.namelist() if fnmatch.fnmatch(n, "ppt/slides/slide*.xml"))
    if not slide_names:
        return f"{path}: no slide XML found inside the archive"
    run_re = re.compile(
        r"<a:r>.*?<a:solidFill><a:srgbClr val=\"([0-9A-Fa-f]{6})\"/?.*?</a:solidFill>.*?"
        r"<a:t>(.*?)</a:t>.*?</a:r>", re.DOTALL)
    text_only_re = re.compile(r"<a:t>(.*?)</a:t>")
    out = []
    for name in slide_names:
        xml = zf.read(name).decode("utf-8", errors="replace")
        runs = run_re.findall(xml)
        if runs:
            for color, text in runs:
                out.append(f"{name}: color=#{color.upper()} text={text!r}")
        else:
            # solidFill/srgbClr may sit in a different XML order than the happy-path regex
            # expects (e.g. run properties split across attributes) -- report the text found
            # so the auditor knows this slide needs a screenshot to judge instead of silently
            # looking like "no colored text" when it might just be an unhandled XML shape.
            texts = text_only_re.findall(xml)
            if texts:
                out.append(f"{name}: color=<not parsed from this XML shape> "
                          f"texts={[t for t in texts if t.strip()]}")
    return "\n".join(out) if out else f"{path}: no text runs found in any slide"


if __name__ == "__main__":
    mcp.run()
