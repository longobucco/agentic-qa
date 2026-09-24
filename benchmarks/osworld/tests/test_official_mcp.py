"""The official `computer` tool registered on the MCP server, plus protocol env forwarded to
both CLIs' MCP children.
"""
import asyncio
import base64
import importlib
import io
import os
import sys
from unittest.mock import patch

from PIL import Image

from benchmarks.osworld import config
from core import codex_loop


def _reset():
    # "from X import Y" (used by every OTHER test module, e.g. test_mcp_server.py) skips
    # reimporting a submodule whenever the parent package still has an attribute for it --
    # sys.modules.pop alone doesn't clear that attribute, so a stale module built under a
    # patched env here would otherwise leak into an unrelated test that runs later in the
    # same process and never expects OSW_PROTOCOL to be set.
    sys.modules.pop("benchmarks.osworld.mcp.server", None)
    pkg = sys.modules.get("benchmarks.osworld.mcp")
    if pkg is not None and hasattr(pkg, "server"):
        delattr(pkg, "server")


def _server(**env):
    _reset()
    with patch.dict(os.environ, env):
        # importlib.import_module (not "from X import Y"): see _reset's docstring -- this goes
        # through the real finder/loader path and always re-executes the module body under the
        # patched env, rather than short-circuiting on a cached parent attribute.
        return importlib.import_module("benchmarks.osworld.mcp.server")


def test_official_registers_only_computer_and_returns_text_image_text():
    s = _server(OSW_MAX_STEPS="5")
    assert [t.name for t in asyncio.run(s.mcp.list_tools())] == ["computer"]
    buf = io.BytesIO()
    Image.new("RGB", (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)).save(buf, format="PNG")
    s._ctrl.screenshot = lambda: buf.getvalue()
    s._ctrl.execute = lambda *a, **k: '{"status": "success"}'
    s._session.sleep = lambda _: None
    res = asyncio.run(s.mcp.call_tool("computer", {"action": "left_click", "coordinate": [1, 2]}))
    content = res[0] if isinstance(res, tuple) else res
    assert [c.type for c in content] == ["text", "image", "text"]
    assert content[2].text == "[Current step: 1/5]"
    assert Image.open(io.BytesIO(base64.b64decode(content[1].data))).size == (
        config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
    _reset()


def test_child_env_forwards_protocol(monkeypatch):
    from benchmarks.osworld.runners import common
    env = common.mcp_child_env()
    assert env["OSW_PROTOCOL"] == "official" and env["OSW_MAX_STEPS"] == str(config.MAX_STEPS)
    cmd = codex_loop.build_codex_cmd("p", model="m", cwd="/tmp", controller_url="http://c",
                                     mcp_extra_env=env)
    assert 'OSW_PROTOCOL = "official"' in " ".join(cmd)
    assert codex_loop.allowed_mcp_tools() == ("computer",)
