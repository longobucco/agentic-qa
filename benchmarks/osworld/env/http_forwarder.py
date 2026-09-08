"""A loopback http:// front door for code that insists on building its own URL.

OSWorld's evaluators reach the guest in two different ways. `PythonController` /
`SetupController` keep the base URL in an attribute we can overwrite, so pointing them at a
Daytona proxy (`https://5000-<id>.daytonaproxy01.eu`) is a one-line patch. Twelve getters do
not: they interpolate the scheme inline, e.g.

    requests.post(f"http://{vm_ip}:{port}/execute", ...)   # getters/general.py

Split an https:// URL into host + port and that becomes `http://<host>:443`, which the proxy's
load balancer answers with `400 Bad Request` and a `text/html` body -- so the getter's
`response.json()` raises `JSONDecodeError` before it ever looks at the status code. Measured
live 2026-09-08 against a real sandbox URL; see docs/finding-sonnet5-oracle-http-scheme.md for
the 20 tasks it cost.

The fix has to make `http://{host}:{port}` *true*, not merely patch the callers we can reach.
This binds a plain-HTTP listener on 127.0.0.1 and forwards every request verbatim to the real
controller URL, whatever its scheme. Hand the getters `vm_ip="127.0.0.1"` and this port and the
inline URL they build is correct by construction -- including any getter added upstream later.

Port 80 is not an alternative: the proxy does answer there, but with a 301 to https, and
`requests` turns a redirected POST into a GET (`Session.rebuild_method`), so every POST getter
would silently query the wrong route.

Still out of reach, by design: getters that ask for a *different guest port* (Chrome's 9222,
VLC's 8080) rather than the controller's. Those ports aren't published by the sandbox at all,
so no URL rewriting can help; they surface as `oracle_unroutable` in G9.
"""
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Setup uploads upstream allows 600s for; scoring getters are far quicker. One generous ceiling
# beats a per-route table that drifts out of sync with upstream.
DEFAULT_TIMEOUT = 900

# Hop-by-hop headers: forwarding these breaks the connection semantics of our own hop.
_SKIP_REQUEST_HEADERS = {"host", "connection", "keep-alive", "proxy-connection",
                         "transfer-encoding", "upgrade"}
_SKIP_RESPONSE_HEADERS = {"connection", "keep-alive", "transfer-encoding", "upgrade"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # BaseHTTPRequestHandler logs every request to stderr; callers capture our output as data.
    def log_message(self, *_args):
        pass

    def _forward(self):
        target = self.server.target_url + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _SKIP_REQUEST_HEADERS}
        req = urllib.request.Request(target, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=self.server.timeout_s) as r:
                self._relay(r.status, r.headers.items(), r.read())
        except urllib.error.HTTPError as e:
            # A non-2xx is an answer, not a transport failure: several getters branch on the
            # status code, so it has to reach them intact rather than becoming a 502 here.
            self._relay(e.code, e.headers.items(), e.read())
        except Exception as e:
            payload = f"forwarder could not reach {target}: {e}".encode()
            self._relay(502, [("Content-Type", "text/plain")], payload)

    def _relay(self, status, headers, body):
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in _SKIP_RESPONSE_HEADERS and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _forward


class LoopbackForwarder:
    """Context manager exposing `host`/`port`/`base_url` for a plain-HTTP loopback front door.

    Usage:
        with LoopbackForwarder(controller_url) as fwd:
            env.vm_ip, env.server_port = fwd.host, fwd.port
    """

    def __init__(self, target_url, *, timeout=DEFAULT_TIMEOUT):
        self.target_url = target_url.rstrip("/")
        self.timeout = timeout
        self._srv = None
        self._thread = None

    @property
    def host(self):
        return "127.0.0.1"

    @property
    def port(self):
        return self._srv.server_address[1] if self._srv else None

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def start(self):
        # Port 0: the OS picks a free one. A fixed port would collide the moment two runs
        # overlap, and this repo already runs scoring from more than one process.
        self._srv = ThreadingHTTPServer((self.host, 0), _Handler)
        self._srv.target_url = self.target_url
        self._srv.timeout_s = self.timeout
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False


def split_for_getters(controller_url, forwarder):
    """(vm_ip, server_port) the upstream getters should be handed.

    The forwarder when it is up; otherwise the old host/port split, so a forwarder that fails
    to bind degrades to the previous behaviour instead of losing scoring altogether."""
    if forwarder is not None and forwarder.port:
        return forwarder.host, forwarder.port
    u = urlparse(controller_url)
    return (u.hostname or "localhost"), (u.port or (443 if u.scheme == "https" else 5000))
