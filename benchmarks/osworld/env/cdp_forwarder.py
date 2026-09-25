"""A local WebSocket-aware relay that makes Chrome's raw CDP port (9222) reachable the same
way env/http_forwarder.py already makes the controller's HTTP port reachable -- fixing the
setup-time half of what G9's replication-validity analysis already tracks as
`oracle_unroutable` for the evaluation-time getters (get_open_tabs_info,
get_active_tab_info, get_active_tab_html_parse -- see g9_replication_validity.py's own
docstring, "the getter reaches for a guest port the sandbox doesn't publish").

Confirmed LIVE 2026-09-13 that the getter/setup-side assumption behind that name is wrong for
Daytona specifically: `sandbox.get_preview_link(9222)` DOES return a real, routable preview
hostname for that port (verified: `GET /json/version` through it returns Chrome's own real
metadata) -- the port is not unpublished at all. Two separate, narrower problems were actually
blocking every CDP connection attempt from the host, both fixed here:

  1. Chrome (~M96+) refuses a WebSocket upgrade whose Origin header doesn't match
     localhost/127.0.0.1 ("Rejected an incoming WebSocket connection from the
     https://... origin. Use ... --remote-allow-origins=... to allow connections from this
     origin"), by design (anti DNS-rebinding). Fixed by `inject_remote_allow_origins()`, called
     wherever a config/postconfig step launches Chrome with --remote-debugging-port -- adds
     --remote-allow-origins=* if not already present.
  2. Even past that, Chrome's own `/json/version` response hardcodes
     "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/<id>" -- ITS OWN view of
     itself, not a routable address from the harness host. playwright.chromium.connect_over_cdp
     (and every vendored desktop_env getter that calls it) trusts that string literally, so even
     a syntactically-correct https://<preview-host> endpoint fails one hop later with
     ECONNREFUSED against literal "localhost". CdpForwarder below is a tiny local WS-aware proxy
     (the WS analogue of LoopbackForwarder in http_forwarder.py) that serves its OWN corrected
     discovery JSON -- rewriting every ws://.../http://... occurrence to point back at itself --
     and relays the actual WebSocket bytes to the real remote endpoint once a client connects.
     Handing vendored code `vm_ip="127.0.0.1"`/`chromium_port=<this forwarder's port>` makes its
     naive `f"http://{vm_ip}:{port}"` + auto-discovery correct by construction, exactly the same
     trick LoopbackForwarder already uses for the controller's own HTTP port.

Gated on `enable_cdp_forwarder` at both call sites (env/sandbox.py's `_run_config` and
env/osworld_eval.py's `evaluate_official`), both on the closed-book path (the only path this
branch runs). On Daytona, true for every caller of each, so CdpForwarder.start() probes
Chrome's CDP port unconditionally and raises CdpForwarderError (caught, logged, harmless) if
nothing is listening yet -- costing nothing on a task that never launches Chrome. On kvm it is
always False (env/kvm_vm.py's setup call, and runners/common.py's scoring call via
`config.BACKEND != "kvm"`): the official VM there publishes Chrome's CDP port on a routable
host port directly, so no forwarder is needed and every consumer uses the mapped port.

No new dependency: built on stdlib socket/ssl only. Message-level WS libraries were available
(websocket-client, a client only) but a byte-for-byte relay after each side completes its OWN
handshake is simpler and correct -- WS frame masking is carried in the frame header itself, not
tied to a specific TCP connection, so splicing raw bytes between two independently-established
WS connections needs no frame parsing at all.
"""
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import threading
import urllib.request
from urllib.parse import urlparse

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CHROME_LAUNCH_NAMES = ("google-chrome", "chromium", "chromium-browser")


class CdpForwarderError(RuntimeError):
    pass


def inject_remote_allow_origins(steps):
    """Return a COPY of `steps` (config or postconfig) where any "launch" step starting a
    Chrome/Chromium binary with --remote-debugging-port also gets --remote-allow-origins=*,
    added only if not already present. Confirmed live 2026-09-13: without this flag Chrome
    answers a proxied WebSocket upgrade with 403 Forbidden regardless of anything on the
    Daytona/network side (see this module's docstring) -- CdpForwarder cannot work around it,
    since it's Chrome itself refusing the connection, not a routing failure."""
    out = []
    for step in steps or ():
        if str(step.get("type", "")).lower() != "launch":
            out.append(step)
            continue
        params = step.get("parameters") or {}
        command = params.get("command")
        if not isinstance(command, list) or not command:
            out.append(step)
            continue
        binary = str(command[0])
        has_debug_port = any(
            str(arg).startswith("--remote-debugging-port") for arg in command[1:])
        has_allow_origins = any(
            str(arg).startswith("--remote-allow-origins") for arg in command)
        if any(name in binary for name in _CHROME_LAUNCH_NAMES) and has_debug_port \
                and not has_allow_origins:
            new_step = dict(step)
            new_params = dict(params)
            new_params["command"] = list(command) + ["--remote-allow-origins=*"]
            new_step["parameters"] = new_params
            out.append(new_step)
        else:
            out.append(step)
    return out


def _read_http_head(sock):
    """Read a request/response head (start line + headers) up to the blank line, byte by
    byte -- CDP discovery/handshake heads are tiny (a few hundred bytes), so this is not a
    throughput concern, and it leaves the socket positioned exactly at the first body/frame
    byte for whatever comes next (JSON body, or raw WS frames)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            raise CdpForwarderError("HTTP head too large")
    head, _, _ = buf.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    start_line = lines[0] if lines else ""
    headers = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return start_line, headers


def _parse_request_line(start_line):
    parts = start_line.split(" ")
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    return method, path


def _write_http_response(sock, status, body, *, content_type="application/json; charset=UTF-8"):
    reason = {200: "OK", 502: "Bad Gateway"}.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    sock.sendall(head + body)


class CdpForwarder:
    """Context manager exposing `host`/`port` for a local WS-aware CDP relay. Usage mirrors
    LoopbackForwarder:

        with CdpForwarder(controller_url, sandbox=sb) as fwd:
            setup_ctrl.vm_ip, setup_ctrl.chromium_port = fwd.host, fwd.port

    `sandbox`: the Daytona sandbox object, used for the authoritative `get_preview_link(port)`
    call -- available at setup time (env/sandbox.py holds it right after provision()). Omit it
    (evaluate_official's call site has no sandbox reference, only a controller_url) to fall
    back to a verified-live URL pattern instead: Daytona's preview hostname is
    f"{port}-{sandbox_id}.{suffix}" for every port on the same sandbox, so substituting the
    guest port for the controller's own known port prefix reconstructs it without the SDK
    object. The substitution is never trusted blindly -- start() probes /json/version through
    it and raises CdpForwarderError immediately if that fails, rather than deferring a
    confusing connection-refused into whatever vendored getter dials in later.
    """

    def __init__(self, controller_url, *, sandbox=None, guest_port=9222, controller_port=None,
                 remote_scheme="https", remote_port=443):
        self.controller_url = controller_url.rstrip("/")
        self.sandbox = sandbox
        self.guest_port = guest_port
        self.controller_port = controller_port
        # Overridable only so tests can point this at a local plain-HTTP mock instead of a
        # real Daytona preview link (always https/443 in production).
        self.remote_scheme = remote_scheme
        self.remote_port = remote_port
        self._remote_host = None
        self._srv_sock = None
        self._accept_thread = None
        self._stop = threading.Event()

    @property
    def host(self):
        return "127.0.0.1"

    @property
    def port(self):
        return self._srv_sock.getsockname()[1] if self._srv_sock else None

    def _resolve_remote_host(self):
        if self.sandbox is not None:
            link = self.sandbox.get_preview_link(self.guest_port).url
            return urlparse(link).hostname
        u = urlparse(self.controller_url)
        host = u.hostname or ""
        prefix = f"{self.controller_port}-"
        if not host.startswith(prefix):
            raise CdpForwarderError(
                f"cannot derive a preview hostname for port {self.guest_port} from "
                f"{self.controller_url!r} (expected it to start with {prefix!r}); pass "
                f"`sandbox=` instead of relying on the URL-pattern fallback")
        return f"{self.guest_port}-" + host[len(prefix):]

    def start(self):
        """Resolves the remote hostname and starts listening -- does NOT probe /json/version
        here. Confirmed live 2026-09-13: this forwarder is constructed and started BEFORE the
        task's own config has launched Chrome (chrome_open_tabs is a LATER step in the same
        setup() call that starts Chrome+socat first) -- an eager liveness probe at start() time
        fires before anything is listening on the guest port yet and fails every single time,
        even though the hostname resolution itself is already correct. get_preview_link() is a
        proxy ROUTE registration, not a liveness check, so resolving the hostname needs no
        network round trip at all when `sandbox` is given. Any real problem (wrong host, target
        never comes up) surfaces clearly and directly on first actual use instead -- see
        _proxy_discovery/_relay_websocket's own error handling -- which is also the earliest
        point a liveness check could be meaningful."""
        self._remote_host = self._resolve_remote_host()
        self._srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv_sock.bind((self.host, 0))
        self._srv_sock.listen(16)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._srv_sock:
            try:
                self._srv_sock.close()
            except OSError:
                pass
            self._srv_sock = None
        if self._accept_thread:
            self._accept_thread.join(timeout=5)
            self._accept_thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv_sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            start_line, headers = _read_http_head(conn)
            _method, path = _parse_request_line(start_line)
            headers_lower = {k.lower(): v for k, v in headers}
            if headers_lower.get("upgrade", "").lower() == "websocket":
                self._relay_websocket(conn, path, headers_lower)
            else:
                self._proxy_discovery(conn, path)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass

    def _proxy_discovery(self, conn, path):
        try:
            with urllib.request.urlopen(
                    f"{self.remote_scheme}://{self._remote_host}:{self.remote_port}{path}",
                    timeout=10) as r:
                body = r.read()
        except Exception as e:
            _write_http_response(conn, 502, json.dumps({"cdp_forwarder_error": str(e)}).encode())
            conn.close()
            return
        # Rewrite every ws://<anything>/... or http://<anything>/... occurrence (Chrome's own
        # "localhost:9222", not ours) to point back at this forwarder -- so the SAME
        # auto-discovery flow every vendored CDP getter already runs connects to us next.
        text = body.decode("utf-8", errors="replace")
        rewritten = re.sub(
            r"(wss?|https?)://[^/\"']+",
            lambda m: ("ws" if m.group(1).startswith("ws") else "http")
                     + f"://{self.host}:{self.port}",
            text,
        )
        _write_http_response(conn, 200, rewritten.encode())
        conn.close()

    def _relay_websocket(self, conn, path, headers_lower):
        key = headers_lower.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        try:
            raw = socket.create_connection((self._remote_host, self.remote_port), timeout=10)
            remote = (ssl.create_default_context().wrap_socket(
                          raw, server_hostname=self._remote_host)
                     if self.remote_scheme == "https" else raw)
            remote_key = base64.b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {self._remote_host}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {remote_key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Origin: https://{self._remote_host}\r\n\r\n"
            )
            remote.sendall(handshake.encode())
            status_line, _ = _read_http_head(remote)
            if " 101 " not in f" {status_line} ":
                raise CdpForwarderError(f"upstream refused WS upgrade: {status_line!r}")
        except Exception as e:
            _write_http_response(conn, 502, str(e).encode())
            conn.close()
            return

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(response.encode())
        _splice(conn, remote)


def _splice(a, b):
    """Bidirectional raw byte relay between two already-upgraded WS sockets, until either
    side closes. No frame parsing: WS masking is self-contained per frame, so pumping bytes
    verbatim between two independently-established connections is correct as-is."""
    def pump(src, dst):
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.close()
                except OSError:
                    pass

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
