"""Pure regression tests for env/host_proxy.py's CA-bundle and env-scoping logic -- no live
mitmdump process, no sandbox. See astra_openbook_campaign_lock.json's known_issues for the two
incidents these encode."""
import os
import tempfile
from pathlib import Path

from benchmarks.osworld.env import host_proxy


def test_combined_ca_bundle_includes_both_the_system_store_and_our_ca():
    """REQUESTS_CA_BUNDLE replaces requests' trust store rather than adding to it -- pointing it
    at only the mitmproxy CA broke TLS verification for the real, legitimately-signed Daytona
    controller reached in the same requests session as a proxied external download."""
    confdir = tempfile.mkdtemp()
    ca_cert = Path(confdir) / "mitmproxy-ca-cert.pem"
    ca_cert.write_text("-----BEGIN CERTIFICATE-----\nFAKE-MITM-CA\n-----END CERTIFICATE-----\n")
    combined = host_proxy._combined_ca_bundle(confdir, ca_cert)
    text = combined.read_text()
    assert "FAKE-MITM-CA" in text
    assert "-----BEGIN CERTIFICATE-----" in text.split("FAKE-MITM-CA")[0], (
        "the system (certifi) bundle must come first, not be replaced by the mitm CA alone")


def test_scoped_env_excludes_the_given_hosts_from_proxying():
    """_download_setup uploads the file it just fetched back to the real Daytona controller in
    the SAME requests session -- a blanket HTTP_PROXY caught that upload too (502 from our own
    fixture proxy, which has no entry for the controller's URL)."""
    handle = {"proxy_url": "http://127.0.0.1:9", "port": 9, "ca_cert": None}
    with host_proxy.scoped_env(handle, no_proxy_hosts=("my-controller.example",)):
        assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
        assert os.environ["NO_PROXY"] == "my-controller.example"
        assert os.environ["no_proxy"] == "my-controller.example"
    assert "NO_PROXY" not in os.environ
    assert "HTTP_PROXY" not in os.environ


def test_scoped_env_restores_prior_values_exactly():
    os.environ["HTTP_PROXY"] = "http://pre-existing:1"
    try:
        handle = {"proxy_url": "http://127.0.0.1:9", "port": 9, "ca_cert": None}
        with host_proxy.scoped_env(handle):
            assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
        assert os.environ["HTTP_PROXY"] == "http://pre-existing:1"
    finally:
        os.environ.pop("HTTP_PROXY", None)


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
