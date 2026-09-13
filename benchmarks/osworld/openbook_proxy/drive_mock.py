"""mitmproxy addon: mock just enough of Google's OAuth2 token endpoint and the Drive v2 API for
`desktop_env.evaluators.getters.chrome.get_googledrive_file` AND
`desktop_env.controllers.setup.SetupController._googledrive_setup` (both pydrive2) to run against
a fixture bundle instead of a real Google account.

Verified live 2026-09-12 with real pydrive2 1.21.3 installed (not just inferred from docs, unlike
the note this replaces -- the original version of this module was written blind, before pydrive2
was ever installed in this repo). Corrections that surfaced against the real library:
  - httplib2 (pydrive2's HTTP layer, not requests) reads lowercase http_proxy/https_proxy, and
    does NOT read REQUESTS_CA_BUNDLE at all -- it needs httplib2.CA_CERTS set explicitly (see
    env/host_proxy.py's scoped_env, which now does this for both).
  - Upload/create goes to a DIFFERENT path than list/get: `POST /upload/drive/v2/files?...`, not
    `POST /drive/v2/files` -- routing on `path.endswith("/files")` alone (the original guess)
    silently misrouted every upload into the list handler, returning a shape the client rejected
    with a 200-status HttpError.
  - `CreateFile().Upload()` defaults to the RESUMABLE upload protocol, not a single multipart
    POST (the original guess): (1) POST .../upload/drive/v2/files?uploadType=resumable with a
    plain JSON metadata body and an X-Upload-Content-Type header, expecting back an empty 200
    with a Location header naming the session; (2) a second request to that Location carrying
    the raw file bytes as its body, expecting back the final {"id": ...} JSON. Confirmed against
    a live googleapiclient request/response pair, not assumed -- the initial (wrong) multipart-
    parsing implementation failed with a 200-status ResumableUploadError since step (1)'s
    response didn't match what next_chunk() requires.

Attached ONLY on the host-side proxy (env/host_proxy.py), alongside addon.py, and only for tasks
tagged `external_oauth_service`: pydrive2 runs on the harness host, never inside the sandbox.
"""
import hashlib
import json
import os
import re
import uuid
from urllib.parse import urlparse, parse_qs, urlencode

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
        # Files created during THIS process's lifetime (_googledrive_setup's 'upload'/'mkdirs'
        # operations) -- not part of the frozen fixture bundle, since they're produced by the
        # setup step itself, not authored ahead of time. Lives only as long as this mitmdump
        # process; fine, since a campaign starts a fresh one per task.
        self.created_files = {}
        # In-flight resumable-upload sessions, keyed by the opaque upload_id this addon mints in
        # step 1's Location header and expects back verbatim in step 2's query string.
        self.pending_uploads = {}

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
        # Confirmed live: create/upload is POST .../upload/drive/v2/files?..., a DIFFERENT path
        # from list/get's .../drive/v2/files -- checked first, and on method too, so a stray GET
        # never gets routed here by a path-substring coincidence. upload_id present means this is
        # step 2 of an already-initiated resumable session, regardless of path shape.
        is_files_collection = parsed.path.rstrip("/").endswith("/files") or "/files?" in path
        if "upload_id" in query:
            self._handle_upload_continue(flow, query["upload_id"][0])
        elif flow.request.method == "POST" and "/upload/drive/" in parsed.path:
            self._handle_upload_init(flow)
        elif flow.request.method == "POST" and is_files_collection:
            # Confirmed live: a metadata-only Upload() (no SetContentFile call -- e.g.
            # mkdir_in_googledrive's folder creation) never touches the resumable protocol at
            # all; pydrive2's own _FilesInsert only attaches media_body (and therefore hits
            # /upload/drive/...) when self.dirty["content"] is set. Without content it POSTs the
            # plain JSON body straight to .../drive/v2/files -- same collection URL a GET here
            # would list, distinguished only by method.
            self._handle_simple_create(flow)
        elif is_files_collection:
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
        for file_id, created in self.created_files.items():
            if title is not None and created["title"] != title:
                continue
            items.append({"id": file_id, "title": created["title"],
                         "mimeType": created["mime_type"]})
        flow.response = http.Response.make(
            200, json.dumps({"items": items}).encode(), {"content-type": "application/json"})

    def _handle_get(self, flow, file_id, query):
        alt = (query.get("alt") or [""])[0]
        created = self.created_files.get(file_id)
        fixture = created or self.bundle.lookup_drive_by_id(file_id)
        if fixture is None:
            flow.response = http.Response.make(
                404, json.dumps({"osw_openbook": "drive_file_miss", "file_id": file_id}).encode(),
                {"content-type": "application/json"})
            return
        title = fixture["title"] if created else fixture.path[-1]
        mime_type = fixture["mime_type"] if created else fixture.mime_type
        body = fixture["body"] if created else fixture.body
        if alt == "media":
            flow.response = http.Response.make(200, body, {"content-type": mime_type})
        else:
            flow.response = http.Response.make(
                200, json.dumps({"id": file_id, "title": title, "mimeType": mime_type}).encode(),
                {"content-type": "application/json"})

    def _handle_simple_create(self, flow):
        """A metadata-only Upload() (no SetContentFile call -- e.g. mkdir_in_googledrive's own
        folder creation): a plain POST straight to the files collection, answered directly with
        the final {"id": ...} JSON -- no resumable session, nothing to continue."""
        title, mime_type = "untitled", "application/octet-stream"
        try:
            meta = json.loads(flow.request.content or b"{}")
            title = meta.get("title") or meta.get("name") or title
            mime_type = meta.get("mimeType") or mime_type
        except json.JSONDecodeError:
            pass
        file_id = hashlib.sha256(f"{title}:{mime_type}:{len(self.created_files)}".encode()) \
            .hexdigest()[:24]
        self.created_files[file_id] = {"title": title, "mime_type": mime_type, "body": b""}
        flow.response = http.Response.make(
            200, json.dumps({"id": file_id, "title": title, "mimeType": mime_type}).encode(),
            {"content-type": "application/json"})

    def _handle_upload_init(self, flow):
        """Step 1 of the resumable protocol (content-bearing Upload() calls only -- see
        _handle_simple_create for the metadata-only case): a plain JSON metadata body (NOT
        multipart -- see the module docstring's correction), answered with an empty 200 and a
        Location header naming the session step 2 must hit."""
        title, mime_type = "untitled", "application/octet-stream"
        try:
            meta = json.loads(flow.request.content or b"{}")
            title = meta.get("title") or meta.get("name") or title
        except json.JSONDecodeError:
            pass
        mime_type = flow.request.headers.get("x-upload-content-type", mime_type)
        upload_id = uuid.uuid4().hex
        self.pending_uploads[upload_id] = {"title": title, "mime_type": mime_type}
        # flow.request.path/parsed above carry only path+query, not scheme+host -- confirmed
        # live: building the Location from that produced "uri = :///..." (empty scheme/netloc),
        # which httplib2 correctly refuses as non-absolute. pretty_url carries the full thing.
        full = urlparse(flow.request.pretty_url)
        location_query = {k: v[0] for k, v in parse_qs(full.query).items()}
        location_query["upload_id"] = upload_id
        location = f"{full.scheme}://{full.netloc}{full.path}?{urlencode(location_query)}"
        flow.response = http.Response.make(200, b"", {"location": location})

    def _handle_upload_continue(self, flow, upload_id):
        """Step 2: the raw file bytes as the whole request body (no multipart framing at this
        stage either), addressed by the upload_id step 1 minted. An unknown upload_id (session
        expired, or a request replayed against a different mitmdump process) is a clean 404
        rather than fabricating a file for content nobody registered metadata for."""
        pending = self.pending_uploads.pop(upload_id, None)
        if pending is None:
            flow.response = http.Response.make(
                404, json.dumps({"osw_openbook": "drive_upload_session_miss",
                                "upload_id": upload_id}).encode(),
                {"content-type": "application/json"})
            return
        body = flow.request.content or b""
        file_id = hashlib.sha256(f"{pending['title']}:{len(body)}:{upload_id}".encode()) \
            .hexdigest()[:24]
        self.created_files[file_id] = {**pending, "body": body}
        flow.response = http.Response.make(
            200, json.dumps({"id": file_id, "title": pending["title"],
                            "mimeType": pending["mime_type"]}).encode(),
            {"content-type": "application/json"})


addons = [DriveMockAddon()]
