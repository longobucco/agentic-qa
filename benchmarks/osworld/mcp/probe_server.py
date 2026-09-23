"""Stub MCP server for analysis/screenshot_delivery_probe.py: a synthetic 1080p screenshot with
one red dot at (OSW_PROBE_X, OSW_PROBE_Y), and report(x, y) to record where the model saw it."""
import io
import json
import os

from PIL import Image, ImageDraw


def dot_png(x, y, w, h):
    img = Image.new("RGB", (w, h), (235, 235, 235))
    ImageDraw.Draw(img).ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    from mcp.server.fastmcp import FastMCP, Image as McpImage

    mcp = FastMCP("osworld")
    X, Y = int(os.environ["OSW_PROBE_X"]), int(os.environ["OSW_PROBE_Y"])
    W, H = int(os.environ.get("OSW_PROBE_W", "1920")), int(os.environ.get("OSW_PROBE_H", "1080"))

    @mcp.tool()
    def screenshot() -> McpImage:
        """PNG of the current screen."""
        return McpImage(data=dot_png(X, Y, W, H), format="png")

    @mcp.tool()
    def report(x: int, y: int) -> str:
        """Report the pixel coordinates of the red dot's center."""
        with open(os.environ["OSW_PROBE_OUT"], "w") as f:
            json.dump({"x": x, "y": y}, f)
        return "recorded"

    mcp.run()
