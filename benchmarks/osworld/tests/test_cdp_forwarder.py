"""Unit tests for env/cdp_forwarder.py (offline: a plain-HTTP loopback mock stands in for
Chrome's own discovery+WS endpoint, no sandbox, no real Chrome):
  python -m benchmarks.osworld.tests.test_cdp_forwarder
"""
import base64
import hashlib
import json
import socket
import threading

from benchmarks.osworld.env.cdp_forwarder import (
    CdpForwarder, CdpForwarderError, _WS_GUID, inject_remote_allow_origins,
)


# --- inject_remote_allow_origins ------------------------------------------------------------

def test_adds_the_flag_to_a_chrome_launch_with_a_debug_port():
    steps = [{"type": "launch",
             "parameters": {"command": ["google-chrome", "--remote-debugging-port=1337"]}}]
    out = inject_remote_allow_origins(steps)
    assert out[0]["parameters"]["command"] == [
        "google-chrome", "--remote-debugging-port=1337", "--remote-allow-origins=*"]
    # original untouched
    assert steps[0]["parameters"]["command"] == ["google-chrome", "--remote-debugging-port=1337"]


def test_does_not_duplicate_an_already_present_flag():
    steps = [{"type": "launch", "parameters": {"command": [
        "google-chrome", "--remote-debugging-port=1337", "--remote-allow-origins=foo"]}}]
    out = inject_remote_allow_origins(steps)
    assert out[0]["parameters"]["command"].count("--remote-allow-origins=foo") == 1
    assert not any(a == "--remote-allow-origins=*" for a in out[0]["parameters"]["command"])


def test_leaves_non_chrome_launches_alone():
    steps = [{"type": "launch", "parameters": {"command": ["socat", "tcp-listen:9222,fork"]}}]
    out = inject_remote_allow_origins(steps)
    assert out == steps


def test_leaves_chrome_launches_without_a_debug_port_alone():
    steps = [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]
    out = inject_remote_allow_origins(steps)
    assert out == steps


def test_leaves_non_launch_steps_alone():
    steps = [{"type": "chrome_open_tabs", "parameters": {"urls_to_open": ["https://x.test"]}}]
    out = inject_remote_allow_origins(steps)
    assert out == steps


# --- CdpForwarder ----------------------------------------------------------------------------

def _accept_key(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


class _MockChromeServer:
    """Stands in for Chrome's own debug port: serves /json/version reporting itself as
    ws://localhost:<fake_port>/devtools/browser/<id> (the exact real-world quirk this module
    exists to work around), and completes a WS handshake at that path, then echoes one
    message back -- enough to prove a relayed round trip actually carries bytes both ways."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        from benchmarks.osworld.env.cdp_forwarder import _read_http_head, _parse_request_line
        try:
            start_line, headers = _read_http_head(conn)
        except Exception:
            conn.close()
            return
        _method, path = _parse_request_line(start_line)
        headers_lower = {k.lower(): v for k, v in headers}
        if headers_lower.get("upgrade", "").lower() == "websocket":
            accept = _accept_key(headers_lower.get("sec-websocket-key", ""))
            conn.sendall((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            # Not a real WS framer -- just echo whatever bytes arrive, which is all the
            # splice-level relay test needs (it never parses frames either).
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    conn.sendall(chunk)
            except OSError:
                pass
            conn.close()
        else:
            body = json.dumps({
                "Browser": "Chrome/999.0 (mock)",
                "webSocketDebuggerUrl": f"ws://localhost:{self.port}/devtools/browser/mockid",
            }).encode()
            conn.sendall((
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            conn.close()

    def stop(self):
        self.sock.close()


class _FakeLink:
    def __init__(self, url):
        self.url = url


class _FakeSandbox:
    """Stands in for the Daytona SDK sandbox object -- get_preview_link is the only method
    CdpForwarder calls on it."""
    def __init__(self, url):
        self._url = url

    def get_preview_link(self, _port):
        return _FakeLink(self._url)


def test_discovery_response_is_rewritten_to_point_back_at_the_forwarder():
    mock = _MockChromeServer()
    try:
        with CdpForwarder("https://5000-fake.example", sandbox=_FakeSandbox(mock.base_url),
                          remote_scheme="http", remote_port=mock.port) as fwd:
            import urllib.request
            with urllib.request.urlopen(f"http://{fwd.host}:{fwd.port}/json/version",
                                        timeout=5) as r:
                info = json.loads(r.read())
            assert info["webSocketDebuggerUrl"] == f"ws://{fwd.host}:{fwd.port}/devtools/browser/mockid"
    finally:
        mock.stop()


def test_a_relayed_websocket_message_round_trips_through_the_mock_server():
    mock = _MockChromeServer()
    try:
        with CdpForwarder("https://5000-fake.example", sandbox=_FakeSandbox(mock.base_url),
                          remote_scheme="http", remote_port=mock.port) as fwd:
            import websocket
            ws = websocket.create_connection(
                f"ws://{fwd.host}:{fwd.port}/devtools/browser/mockid", timeout=5)
            try:
                ws.send("hello-through-the-relay")
                assert ws.recv() == "hello-through-the-relay"
            finally:
                ws.close()
    finally:
        mock.stop()


def test_url_pattern_fallback_substitutes_the_controller_port_prefix():
    fwd = CdpForwarder(
        "https://5000-abc123.daytonaproxy01.eu", guest_port=9222, controller_port=5000)
    assert fwd._resolve_remote_host() == "9222-abc123.daytonaproxy01.eu"


def test_url_pattern_fallback_raises_clearly_when_the_prefix_does_not_match():
    fwd = CdpForwarder("https://unexpected-host.example", guest_port=9222, controller_port=5000)
    try:
        fwd._resolve_remote_host()
        assert False, "expected CdpForwarderError"
    except CdpForwarderError:
        pass


def main():
    tests = [(name, fn) for name, fn in globals().items()
            if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
