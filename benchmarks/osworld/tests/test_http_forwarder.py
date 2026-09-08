"""Unit tests for env/http_forwarder.py (offline: a real loopback origin, no sandbox):
  python -m benchmarks.osworld.tests.test_http_forwarder
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.osworld.env.http_forwarder import LoopbackForwarder, split_for_getters


class _Origin(BaseHTTPRequestHandler):
    """Stands in for the in-guest controller: echoes what it received."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _reply(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Origin-Saw", self.command)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/boom":
            return self._reply(500, {"error": "kaboom"})
        self._reply(200, {"path": self.path, "method": "GET"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        self._reply(200, {"path": self.path, "method": "POST", "body": body,
                          "ctype": self.headers.get("Content-Type")})


def _origin():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


def test_a_get_reaches_the_origin_with_its_path_intact():
    srv, target = _origin()
    try:
        with LoopbackForwarder(target) as fwd:
            status, body = _get(f"{fwd.base_url}/screenshot?x=1")
        assert status == 200 and body["path"] == "/screenshot?x=1"
    finally:
        srv.shutdown()


def test_a_post_keeps_its_method_and_body():
    """The whole point: get_vm_command_line POSTs a JSON command. Port 80 on the real proxy
    answers with a 301 and requests downgrades a redirected POST to GET -- this must not."""
    srv, target = _origin()
    try:
        with LoopbackForwarder(target) as fwd:
            req = urllib.request.Request(
                f"{fwd.base_url}/execute", data=json.dumps({"command": ["ls"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read())
                assert r.headers["X-Origin-Saw"] == "POST"
        assert body["method"] == "POST"
        assert json.loads(body["body"]) == {"command": ["ls"]}
        assert body["ctype"] == "application/json"
    finally:
        srv.shutdown()


def test_a_non_2xx_reaches_the_caller_as_itself_not_as_a_transport_error():
    """Several getters branch on response.status_code; turning a 500 into a 502 here would
    change their answer."""
    srv, target = _origin()
    try:
        with LoopbackForwarder(target) as fwd:
            try:
                _get(f"{fwd.base_url}/boom")
                assert False, "expected an HTTPError"
            except urllib.error.HTTPError as e:
                assert e.code == 500
                assert json.loads(e.read())["error"] == "kaboom"
    finally:
        srv.shutdown()


def test_an_unreachable_origin_is_a_502_not_a_hang_or_a_crash():
    srv, target = _origin()
    srv.shutdown()
    srv.server_close()                  # close the socket too: origin gone, not just idle
    with LoopbackForwarder(target) as fwd:
        try:
            _get(f"{fwd.base_url}/screenshot")
            assert False, "expected an HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 502


def test_the_getter_address_is_the_forwarder_while_it_is_up():
    srv, target = _origin()
    try:
        with LoopbackForwarder(target) as fwd:
            assert split_for_getters("https://5000-abc.daytonaproxy01.eu", fwd) == \
                ("127.0.0.1", fwd.port)
    finally:
        srv.shutdown()


def test_without_a_forwarder_it_degrades_to_the_old_split():
    """Not an endorsement of that split -- it is the bug -- but losing scoring entirely because
    a socket wouldn't bind is worse than reproducing the previous behaviour."""
    assert split_for_getters("https://5000-abc.daytonaproxy01.eu", None) == \
        ("5000-abc.daytonaproxy01.eu", 443)
    assert split_for_getters("http://10.0.0.4:5000", None) == ("10.0.0.4", 5000)


def test_stopping_the_forwarder_releases_the_port():
    srv, target = _origin()
    try:
        fwd = LoopbackForwarder(target).start()
        url = f"{fwd.base_url}/screenshot"
        assert _get(url)[0] == 200
        fwd.stop()
        try:
            _get(url)
            assert False, "forwarder still answering after stop()"
        except Exception:
            pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
        print("ok", name)
