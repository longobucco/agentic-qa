"""mitmproxy script: serve only fixtures from the loaded bundle, never forward anything else.

Run as `mitmdump -p <port> -s benchmarks/osworld/openbook_proxy/addon.py`, with:
  OSW_OPENBOOK_BUNDLE  -- path to a fixture bundle directory (bundle.py's on-disk format)
  OSW_OPENBOOK_MISS_LOG -- path to append one JSON line per unresolved request (optional)

Every request gets a response set in `request()` -- mitmproxy never proceeds to actually connect
upstream once a flow's `response` is set, so a fixture miss becomes an explicit diagnostic page,
not a live fetch. This is the harness's whole "controlled snapshot, not the public Internet"
guarantee; it is enforced HERE, not by anything the addon's caller does.

Requires mitmproxy (not installed in the dev .venv used to author/test this repo -- see
docs/g_astra_open_book_runner_implementation.md's "Explicitly deferred" note: this module is
exercised by the live canary, not by the unit test suite).
"""
import json
import os
import time

from mitmproxy import http

try:
    from benchmarks.osworld.openbook_proxy.bundle import FixtureBundle
except ImportError:
    # Pushed flat into the guest (env/guest_proxy.py) as addon.py + bundle.py side by side --
    # our package isn't installed in the OSWorld desktop image, only these two files are.
    from bundle import FixtureBundle

_MISS_STATUS = 502
_MISS_HEADERS = {"content-type": "application/json", "x-osw-openbook": "miss"}


def _log_miss(record):
    path = os.environ.get("OSW_OPENBOOK_MISS_LOG")
    if not path:
        return
    record = {**record, "at": time.time()}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


class FixtureAddon:
    def __init__(self):
        self.bundle = None
        self.load_error = None
        bundle_dir = os.environ.get("OSW_OPENBOOK_BUNDLE")
        if not bundle_dir:
            self.load_error = "OSW_OPENBOOK_BUNDLE not set"
            return
        try:
            self.bundle = FixtureBundle.load(bundle_dir)
        except Exception as e:  # noqa: BLE001 -- any load failure must degrade to explicit misses
            self.load_error = f"{type(e).__name__}: {e}"

    def request(self, flow: "http.HTTPFlow"):
        method = flow.request.method
        url = flow.request.pretty_url
        if self.bundle is None:
            flow.response = http.Response.make(
                _MISS_STATUS,
                json.dumps({"osw_openbook": "bundle_unavailable",
                           "error": self.load_error}).encode(),
                _MISS_HEADERS,
            )
            _log_miss({"kind": "bundle_unavailable", "method": method, "url": url,
                      "error": self.load_error})
            return
        fixture = self.bundle.lookup_http(method, url)
        if fixture is None:
            flow.response = http.Response.make(
                _MISS_STATUS,
                json.dumps({"osw_openbook": "fixture_miss", "method": method,
                           "url": url}).encode(),
                _MISS_HEADERS,
            )
            _log_miss({"kind": "fixture_miss", "method": method, "url": url})
            return
        flow.response = http.Response.make(fixture.status, fixture.body, fixture.headers)


addons = [FixtureAddon()]
