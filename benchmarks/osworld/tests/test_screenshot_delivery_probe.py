import io
import json
import re

from PIL import Image

from benchmarks.osworld import config
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


def test_probe_writes_a_timestamped_record_under_probes_official(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    rec = {"cli": "claude", "trials": 1, "answered": 1, "points": []}
    dest = probe.write_record(rec, "claude")
    assert dest.parent == tmp_path / "_probes_official"
    assert dest.parent.is_dir()
    assert not (tmp_path / "_probes").exists()
    assert re.fullmatch(r"screenshot_delivery_claude_\d{8}T\d{6}Z\.json", dest.name)
    assert json.loads(dest.read_text()) == rec


def test_probe_refuses_to_overwrite_an_existing_record(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    dest_dir = tmp_path / "_probes_official"
    dest_dir.mkdir(parents=True)
    existing = dest_dir / "screenshot_delivery_claude_20260101T000000Z.json"
    existing.write_text("{}")
    monkeypatch.setattr(probe, "_utc_stamp", lambda: "20260101T000000Z")
    import pytest
    with pytest.raises(FileExistsError):
        probe.write_record({"cli": "claude"}, "claude")
