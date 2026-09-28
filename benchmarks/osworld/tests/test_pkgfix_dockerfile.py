"""Pins the provenance of the pkgfix layer (guest image re-pin, 26/09/2026): the thin Dockerfile
on top of the previous digest installs exactly the six commands the audit found missing, and the
full-rebuild Dockerfile carries the same six so a future rebuild doesn't regress them."""
from pathlib import Path

_DOCKER = Path(__file__).resolve().parents[1] / "docker"
_PREVIOUS_DIGEST = ("ghcr.io/longobucco/osworld-ab@sha256:"
                    "759b54f8b15fbb03544f8ac193ce86115d935a3dd8554a1c44b30f904ed07ea2")
_PACKAGES = {"unzip", "zip", "jq", "psmisc", "xsel", "gnome-terminal"}


def test_pkgfix_dockerfile_is_from_the_previous_pinned_digest():
    text = (_DOCKER / "Dockerfile.osworld-pkgfix").read_text()
    from_lines = [l.strip() for l in text.splitlines() if l.strip().startswith("FROM ")]
    assert from_lines == [f"FROM {_PREVIOUS_DIGEST}"]


def test_pkgfix_dockerfile_installs_exactly_the_six_missing_packages():
    text = (_DOCKER / "Dockerfile.osworld-pkgfix").read_text()
    assert "unzip zip jq psmisc xsel gnome-terminal" in text
    installed = set("unzip zip jq psmisc xsel gnome-terminal".split())
    assert installed == _PACKAGES


def test_full_rebuild_dockerfile_carries_the_same_six_packages():
    text = (_DOCKER / "Dockerfile.osworld").read_text()
    assert "unzip zip jq psmisc xsel gnome-terminal" in text
