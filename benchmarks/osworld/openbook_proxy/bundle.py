"""Fixture bundle format shared by the guest and host proxy addons, the manifest generator, and
preflight. One bundle per task: a `manifest.json` naming every HTTP fixture and every synthetic
Google Drive file, plus content-addressed body files under `bodies/`.

Kept dependency-free (stdlib only) so bundle.py can be imported and unit-tested without mitmproxy
installed -- only addon.py/drive_mock.py need the real proxy library.
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode

SCHEMA_VERSION = 1
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Shared with drive_mock.py (mitmproxy addon) and the runner's own redaction scan -- kept here,
# not in drive_mock.py, so the scan can import it without pulling in mitmproxy (drive_mock.py has
# a module-level `from mitmproxy import http`; this module is stdlib-only by design).
MOCK_ACCESS_TOKEN = "osw-openbook-mock-token"


def canonical_key(method, url):
    """(METHOD, scheme://host[:port]/path?sorted-query) -- normalizes only what's genuinely
    volatile (default port, query-param order); path case and trailing slash are left alone
    since OSWorld fixtures are pinned to one exact task instruction, not a live crawl."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    netloc_path = f"{scheme}://{host}{path}"
    if query:
        netloc_path = f"{netloc_path}?{query}"
    return (method.upper(), netloc_path)


class HttpFixture:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body


class DriveFixture:
    __slots__ = ("file_id", "path", "mime_type", "body")

    def __init__(self, file_id, path, mime_type, body):
        self.file_id = file_id
        self.path = path
        self.mime_type = mime_type
        self.body = body


def drive_file_id(path):
    """Deterministic synthetic Drive file id -- pydrive2 treats it as an opaque string, so a
    stable hash of the declared path is enough; no real Drive API call ever mints one."""
    return hashlib.sha256("/".join(path).encode()).hexdigest()[:24]


class FixtureBundle:
    """Loaded, read-only view of one task's bundle directory. `misses` accumulates every lookup
    that found nothing, for both proxy sites to report back into network_provenance.json /
    preflight -- a miss must never silently fall through to a live fetch (see addon.py)."""

    def __init__(self, task_id, manifest, root):
        self.task_id = task_id
        self.manifest = manifest
        self.root = root
        self._http = {}
        self._drive_by_id = {}
        self.misses = []
        for entry in manifest.get("http", []):
            key = canonical_key(entry["method"], entry["url"])
            self._http[key] = HttpFixture(
                entry["status"], dict(entry.get("headers") or {}),
                self._read_body(entry["body_file"]),
            )
        for entry in manifest.get("drive", []):
            path = list(entry["path"])
            file_id = entry.get("file_id") or drive_file_id(path)
            self._drive_by_id[file_id] = DriveFixture(
                file_id, path, entry.get("mime_type", "application/octet-stream"),
                self._read_body(entry["body_file"]),
            )

    def _read_body(self, rel_path):
        return (self.root / rel_path).read_bytes()

    @classmethod
    def load(cls, bundle_dir):
        bundle_dir = Path(bundle_dir)
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported fixture manifest schema_version={manifest.get('schema_version')!r} "
                f"in {bundle_dir} (expected {SCHEMA_VERSION})"
            )
        return cls(manifest["task_id"], manifest, bundle_dir)

    def lookup_http(self, method, url):
        hit = self._http.get(canonical_key(method, url))
        if hit is None:
            self.misses.append({"kind": "http", "method": method, "url": url})
        return hit

    def lookup_drive_by_id(self, file_id):
        hit = self._drive_by_id.get(file_id)
        if hit is None:
            self.misses.append({"kind": "drive_get", "file_id": file_id})
        return hit

    def find_drive_by_path(self, path):
        """Linear scan is fine: a task's bundle holds a handful of Drive fixtures, never a real
        directory tree."""
        for fixture in self._drive_by_id.values():
            if fixture.path == list(path):
                return fixture
        self.misses.append({"kind": "drive_lookup", "path": list(path)})
        return None

    def sha256(self):
        """Digest over the manifest alone: every body is already content-addressed by its own
        body_sha256 field, so hashing the (canonical, sorted-key) manifest JSON is equivalent to
        hashing the whole bundle without re-reading every body file."""
        canonical = json.dumps(self.manifest, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def build_manifest(task_id, *, http=(), drive=()):
    """Assemble a manifest dict from in-memory entries -- used by the fixture-authoring step and
    by tests. Each `http` entry: {method, url, status, headers, body}. Each `drive` entry:
    {path, mime_type, body}. Writes nothing; pair with write_bundle to persist."""
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "http": [
            {"method": e["method"], "url": e["url"], "status": e.get("status", 200),
             "headers": dict(e.get("headers") or {}),
             "body_sha256": hashlib.sha256(e["body"]).hexdigest(),
             "body_file": f"bodies/{hashlib.sha256(e['body']).hexdigest()}"}
            for e in http
        ],
        "drive": [
            {"path": list(e["path"]), "mime_type": e.get("mime_type", "application/octet-stream"),
             "file_id": e.get("file_id") or drive_file_id(e["path"]),
             "body_sha256": hashlib.sha256(e["body"]).hexdigest(),
             "body_file": f"bodies/{hashlib.sha256(e['body']).hexdigest()}"}
            for e in drive
        ],
    }


def write_bundle(bundle_dir, manifest, bodies):
    """`bodies`: {body_file_relpath: bytes}, as produced alongside build_manifest's entries.
    Content-addressed, so writing the same body twice is a harmless overwrite of identical bytes."""
    bundle_dir = Path(bundle_dir)
    (bundle_dir / "bodies").mkdir(parents=True, exist_ok=True)
    for rel_path, data in bodies.items():
        (bundle_dir / rel_path).write_bytes(data)
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
