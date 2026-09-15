"""Unit tests for env/controller.py's retry wrapper (offline: a real loopback origin that
drops the connection N times before answering, no sandbox):
  python -m benchmarks.osworld.tests.test_controller
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import benchmarks.osworld.env.controller as controller_mod
from benchmarks.osworld.env.controller import Controller


class _FlakyOrigin(BaseHTTPRequestHandler):
    """Drops the connection (no response at all) for the first `drops_left` requests to
    /execute, then answers normally -- mimics the live RemoteDisconnected seen against
    Daytona's preview proxy mid-bundle-push."""
    protocol_version = "HTTP/1.1"
    drops_left = 0
    calls = 0

    def log_message(self, *_a):
        pass

    def do_POST(self):
        type(self).calls += 1
        if self.path == "/execute" and type(self).drops_left > 0:
            type(self).drops_left -= 1
            self.connection.close()
            return
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"output": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_execute_retries_past_a_dropped_connection_and_succeeds():
    _FlakyOrigin.drops_left = 2
    _FlakyOrigin.calls = 0
    server = _serve(_FlakyOrigin)
    try:
        ctrl = Controller(f"http://127.0.0.1:{server.server_port}")
        out = ctrl.execute("echo hi", shell=True)
        assert out == "ok"
        assert _FlakyOrigin.calls == 3  # 2 drops + 1 real success
    finally:
        server.shutdown()


def test_execute_gives_up_after_exhausting_retries():
    _FlakyOrigin.drops_left = 99
    _FlakyOrigin.calls = 0
    server = _serve(_FlakyOrigin)
    try:
        ctrl = Controller(f"http://127.0.0.1:{server.server_port}")
        try:
            ctrl.execute("echo hi", shell=True)
            assert False, "should have raised after exhausting retries"
        except controller_mod._RETRYABLE:
            pass
        assert _FlakyOrigin.calls == controller_mod._RETRIES
    finally:
        server.shutdown()


class _SlowOrigin(BaseHTTPRequestHandler):
    """Accepts the connection but stalls past the client's read timeout for the first
    `stalls_left` requests, then answers promptly -- mimics the live "TimeoutError: The read
    operation timed out" seen mid-bundle-push on 2026-09-15, distinct from a dropped connection
    (RemoteDisconnected): here the socket stays open, the response body just never arrives in
    time."""
    protocol_version = "HTTP/1.1"
    stalls_left = 0
    calls = 0

    def log_message(self, *_a):
        pass

    def do_POST(self):
        type(self).calls += 1
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if type(self).stalls_left > 0:
            type(self).stalls_left -= 1
            time.sleep(0.3)  # longer than the client's timeout below
        body = json.dumps({"output": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_execute_retries_past_a_read_timeout_and_succeeds():
    _SlowOrigin.stalls_left = 1
    _SlowOrigin.calls = 0
    server = _serve(_SlowOrigin)
    try:
        ctrl = Controller(f"http://127.0.0.1:{server.server_port}")
        out = ctrl.execute("echo hi", shell=True, timeout=0.1)
        assert out == "ok"
        assert _SlowOrigin.calls == 2  # 1 timeout + 1 real success
    finally:
        server.shutdown()


def test_a_clean_call_needs_no_retry():
    _FlakyOrigin.drops_left = 0
    _FlakyOrigin.calls = 0
    server = _serve(_FlakyOrigin)
    try:
        ctrl = Controller(f"http://127.0.0.1:{server.server_port}")
        out = ctrl.execute("echo hi", shell=True)
        assert out == "ok"
        assert _FlakyOrigin.calls == 1
    finally:
        server.shutdown()


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    controller_mod._RETRY_DELAY_S = 0.01  # keep the exhausted-retries test fast
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    main()
