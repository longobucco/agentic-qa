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


def test_other_suffixes_keep_the_default_or_override():
    assert config.resolve_image("", ("protocol361img3", "")) == config._DEFAULT_IMAGE
    assert config.resolve_image("", ("", "")) == config._DEFAULT_IMAGE
    assert config.resolve_image("reg/img@sha256:abc", ("x",)) == "reg/img@sha256:abc"


def test_mismatched_image_or_suffix_is_refused():
    with pytest.raises(SystemExit):
        config.resolve_image(config._DEFAULT_IMAGE, ("pkgfix3",))
    with pytest.raises(SystemExit):
        config.resolve_image(PKGFIX3, ("protocol361img3",))
    with pytest.raises(SystemExit):
        config.resolve_image(PKGFIX3, ("",))


def test_config_import_uses_the_suffix_image():
    env = {"OSW_MODEL": "claude-sonnet-5", "OSW_IMAGE": "", "OSW_SYSTEM_SUFFIX": "pkgfix3",
           "OSW_ASTRA_SYSTEM_SUFFIX": ""}
    with patch.dict(os.environ, env):
        image = importlib.reload(config).IMAGE
    importlib.reload(config)
    assert image == PKGFIX3
