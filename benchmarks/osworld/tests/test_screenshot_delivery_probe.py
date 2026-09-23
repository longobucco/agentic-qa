import io

from PIL import Image

from benchmarks.osworld.analysis import screenshot_delivery_probe as probe
from benchmarks.osworld.mcp import probe_server


def test_dot_png_has_the_dot_where_asked():
    img = Image.open(io.BytesIO(probe_server.dot_png(1500, 900, 1920, 1080))).convert("RGB")
    assert img.size == (1920, 1080)
    assert img.getpixel((1500, 900)) == (255, 0, 0)
    assert img.getpixel((100, 100)) != (255, 0, 0)


def test_verdict_accepts_accurate_answers():
    pts = [((1500, 900), (1503, 898)), ((200, 150), (199, 152)), ((960, 540), (961, 540))]
    v = probe.verdict(pts)
    assert v["ok"] and v["mean_error_px"] < 5


def test_verdict_flags_a_systematic_downscale():
    pts = [((1500, 900), (1050, 630)), ((200, 150), (140, 105)), ((960, 540), (672, 378))]
    v = probe.verdict(pts)
    assert not v["ok"] and abs(v["ratio_x"] - 0.7) < 0.01
