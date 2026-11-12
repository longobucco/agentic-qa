import importlib
import os
from unittest.mock import patch

import pytest

from benchmarks.osworld import config

PKGFIX3 = config.SUFFIX_IMAGES["pkgfix3"]


def test_suffix_selects_its_image():
    assert config.resolve_image("", ("pkgfix3", "")) == PKGFIX3
    assert config.resolve_image("", ("", "pkgfix3")) == PKGFIX3
    assert config.resolve_image(PKGFIX3, ("pkgfix3",)) == PKGFIX3


def test_declared_historical_suffixes_keep_the_default_image():
    for suffix in config.DEFAULT_IMAGE_SUFFIXES:
        assert config.resolve_image("", (suffix, "")) == config._DEFAULT_IMAGE
    assert config.resolve_image("", ("", "")) == config._DEFAULT_IMAGE


def test_an_undeclared_suffix_is_refused_instead_of_falling_back():
    """The failure this prevents was observed live on 12/11: passing the DEV1 suffix through the
    wrong environment variable left the image on the Phase-2 digest with no error, so a whole
    tranche would have run on the wrong guest and only the provenance would have shown it."""
    for bad in ("x", "pkgfix3d", "pkgfix3cdev2", "PKGFIX3C"):
        with pytest.raises(SystemExit) as e:
            config.resolve_image("", (bad,))
        assert "not declared" in str(e.value)
    # not even with an explicit image: the suffix decides the guest, not the environment
    with pytest.raises(SystemExit):
        config.resolve_image("reg/img@sha256:abc", ("x",))
    # and a declared suffix mixed with an undeclared one is still refused
    with pytest.raises(SystemExit):
        config.resolve_image("", ("pkgfix3", "typo"))


def test_mismatched_image_or_suffix_is_refused():
    with pytest.raises(SystemExit):
        config.resolve_image(config._DEFAULT_IMAGE, ("pkgfix3",))
    with pytest.raises(SystemExit):
        config.resolve_image(PKGFIX3, ("protocol361img3",))
    with pytest.raises(SystemExit):
        config.resolve_image(PKGFIX3, ("",))


def test_the_confirmatory_suffix_resolves_to_the_same_guest_as_pkgfix3():
    """X22 runs on the same image as the exploratory phase, in its own tree. If these two ever
    diverge, the confirmatory campaign is comparing arms across two different guests."""
    assert config.SUFFIX_IMAGES["pkgfix3c"] == PKGFIX3
    assert config.resolve_image("", ("pkgfix3c", "")) == PKGFIX3
    assert config.resolve_image("", ("", "pkgfix3c")) == PKGFIX3
    # the two suffixes together must not look like a conflict (same digest, so one pinned image)
    assert config.resolve_image("", ("pkgfix3", "pkgfix3c")) == PKGFIX3


def test_an_unmapped_confirmatory_suffix_is_now_refused():
    """This test used to assert the opposite -- that an unmapped suffix falls back to the Phase-2
    image -- as a documented hazard. It is no longer a hazard: it is refused."""
    with pytest.raises(SystemExit):
        config.resolve_image("", ("pkgfix3cNOTMAPPED",))

def test_config_import_uses_the_suffix_image():
    env = {"OSW_MODEL": "claude-sonnet-5", "OSW_IMAGE": "", "OSW_SYSTEM_SUFFIX": "pkgfix3",
           "OSW_ASTRA_SYSTEM_SUFFIX": ""}
    with patch.dict(os.environ, env):
        image = importlib.reload(config).IMAGE
    importlib.reload(config)
    assert image == PKGFIX3


def test_the_dev1_tranche_has_its_own_suffix_on_the_same_guest():
    """DEV1 must never write into an X22 tree: `pkgfix3c` is the confirmatory campaign's own
    suffix, and Gate B scans those trees. A suffix missing from SUFFIX_IMAGES falls back to the
    Phase-2 digest silently, so the tranche would also run on the wrong guest."""
    dev = config.SUFFIX_IMAGES["pkgfix3cdev1"]
    assert dev == PKGFIX3 == config.SUFFIX_IMAGES["pkgfix3c"]
    assert config.resolve_image("", ("pkgfix3cdev1",)) == dev
    assert "pkgfix3cdev1" != "pkgfix3c"


def test_the_dev1_tree_name_is_distinct_from_every_x22_tree(monkeypatch):
    import json
    from pathlib import Path
    lock = json.loads((Path(config.__file__).resolve().parent
                       / "x22_campaign_lock.json").read_text())
    monkeypatch.setattr(config, "ASTRA_SYSTEM_SUFFIX", "pkgfix3cdev1")
    monkeypatch.setattr(config, "ASTRA_ARM", "")
    name = config.astra_system_name()
    assert name not in {g["tree"] for g in lock["groups"]}, name
