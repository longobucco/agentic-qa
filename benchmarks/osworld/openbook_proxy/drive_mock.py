"""mitmproxy addon: mock just enough of Google's OAuth2 token endpoint and the Drive v2 API for
`desktop_env.evaluators.getters.chrome.get_googledrive_file` (pydrive2) to run against a fixture
bundle instead of a real Google account.

INFERRED, NOT OBSERVED: pydrive2 is not installed anywhere in this repo (confirmed during
planning -- `python3 -c "import pydrive2"` fails in .venv), so the exact request shapes below are
reconstructed from pydrive2's documented default (Drive API v2, OAuth2 token endpoint), not from a
captured real trace. Treat every canary run against this addon as the first real verification of
this file, and expect to adjust it (see docs/g_astra_open_book_runner_implementation.md's
"Explicitly deferred" note -- this module is called out there by name as the highest-uncertainty
piece, kept isolated behind the `external_oauth_service` manifest tag for exactly this reason).

Attached ONLY on the host-side proxy (env/host_proxy.py), alongside addon.py, and only for tasks
tagged `external_oauth_service`: pydrive2 runs on the harness host, never inside the sandbox.
"""
import json
import os
import re
import time
from urllib.parse import urlparse, parse_qs

from mitmproxy import http

try:
    from benchmarks.osworld.openbook_proxy.bundle import MOCK_ACCESS_TOKEN, FixtureBundle
except ImportError:
    from bundle import MOCK_ACCESS_TOKEN, FixtureBundle   # host-only today, same fallback as addon.py

_TOKEN_HOSTS = {"oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com"}
# pydrive2's default Drive service is v2; `q` values look like:
#   ( title = 'name' and mimeType = '...' ) and 'parent_id' in parents
_TITLE_RE = re.compile(r"title\s*=\s*'([^']*)'")


class DriveMockAddon:
    def __init__(self):
        self.bundle = None
        bundle_dir = os.environ.get("OSW_OPENBOOK_BUNDLE")
        if bundle_dir:
            try:
                self.bundle = FixtureBundle.load(bundle_dir)
            except Exception:
                self.bundle = None

    def _is_token_request(self, flow):
        host = (flow.request.pretty_host or "").lower()
        return host in _TOKEN_HOSTS and (
            "/token" in flow.request.path or "/o/oauth2/token" in flow.request.path
        )

    def _is_drive_request(self, flow):
        host = (flow.request.pretty_host or "").lower()
        return host == "www.googleapis.com" and "/drive/" in flow.request.path

    def request(self, flow: "http.HTTPFlow"):
        if self._is_token_request(flow):
            flow.response = http.Response.make(
                200,
                json.dumps({"access_token": MOCK_ACCESS_TOKEN, "expires_in": 3600,
                           "token_type": "Bearer"}).encode(),
                {"content-type": "application/json"},
            )
            return
        if not self._is_drive_request(flow):
            return   # not ours -- addon.py's own FixtureAddon (attached alongside) handles it
        self._handle_drive(flow)

    def _handle_drive(self, flow):
        path = flow.request.path
        if self.bundle is None:
            flow.response = http.Response.make(
                502, json.dumps({"osw_openbook": "drive_mock_bundle_unavailable"}).encode(),
                {"content-type": "application/json"})
            return
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        if parsed.path.rstrip("/").endswith("/files") or "/files?" in path:
            self._handle_list(flow, query)
        else:
            file_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            self._handle_get(flow, file_id, query)

    def _handle_list(self, flow, query):
        q = (query.get("q") or [""])[0]
        title_match = _TITLE_RE.search(q)
        title = title_match.group(1) if title_match else None
        items = []
        for fixture in list(self.bundle._drive_by_id.values()):   # read-only scan, own module
            if title is not None and fixture.path[-1] != title:
                continue
            items.append({"id": fixture.file_id, "title": fixture.path[-1],
                         "mimeType": fixture.mime_type})
        flow.response = http.Response.make(
            200, json.dumps({"items": items}).encode(), {"content-type": "application/json"})

    def _handle_get(self, flow, file_id, query):
        alt = (query.get("alt") or [""])[0]
        fixture = self.bundle.lookup_drive_by_id(file_id)
        if fixture is None:
            flow.response = http.Response.make(
                404, json.dumps({"osw_openbook": "drive_file_miss", "file_id": file_id}).encode(),
                {"content-type": "application/json"})
            return
        if alt == "media":
            flow.response = http.Response.make(
                200, fixture.body, {"content-type": fixture.mime_type})
        else:
            flow.response = http.Response.make(
                200, json.dumps({"id": fixture.file_id, "title": fixture.path[-1],
                                "mimeType": fixture.mime_type}).encode(),
                {"content-type": "application/json"})


addons = [DriveMockAddon()]
