"""HTTP client to OSWorld's in-guest server (desktop_env/server/main.py).

Routes verified against upstream: GET /screenshot, GET /accessibility, POST /run_python {code},
POST /execute {command,shell}, POST /file (form file_path) -> raw bytes.
"""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# Daytona's own SDK retries transient connection drops against its control plane (confirmed
# live 2026-09-14: "Retrying ... after connection broken by RemoteDisconnected" against
# /api/sandbox/<id>) -- but urllib.request.urlopen here does not, and guest_proxy.start() issues
# hundreds of sequential calls pushing one large fixture bundle (env/guest_proxy.py's chunked
# writes). A single dropped connection anywhere in that sequence previously killed the whole
# task as an unretried ENVIRONMENT_ERROR; confirmed live in a real 10-task batch (2/10 failed
# with this exact RemoteDisconnected). A small retry here mirrors the resilience Daytona's own
# client already assumes is necessary against the same infrastructure.
_RETRYABLE = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError,
              urllib.error.URLError)
_RETRIES = 3
_RETRY_DELAY_S = 1.0


def _with_retry(fn):
    last = None
    for attempt in range(_RETRIES):
        try:
            return fn()
        except _RETRYABLE as e:
            last = e
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_DELAY_S)
    raise last


class Controller:
    def __init__(self, base_url, *, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, *, raw=False):
        def call():
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as r:
                return r.read()
        data = _with_retry(call)
        return data if raw else data.decode()

    def _post_json(self, path, payload, *, timeout=None):
        def call():
            req = urllib.request.Request(
                f"{self.base_url}{path}", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read().decode()
        return _with_retry(call)

    def _post_form(self, path, form, *, timeout=None):
        def call():
            req = urllib.request.Request(
                f"{self.base_url}{path}", data=urllib.parse.urlencode(form).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        return _with_retry(call)

    def screenshot(self):
        return self._get("/screenshot", raw=True)

    def a11y_tree(self):
        return self._get("/accessibility")

    def pyautogui(self, code):
        # actions run through /run_python (there is no /pyautogui route)
        return self._post_json("/run_python", {"code": code})

    def execute(self, command, *, shell=False, timeout=120):
        out = self._post_json("/execute", {"command": command, "shell": shell}, timeout=timeout)
        try:
            return json.loads(out).get("output", out)
        except Exception:
            return out

    def read_file(self, path):
        return self._post_form("/file", {"file_path": path})   # returns raw bytes

    def ready(self):
        try:
            self._get("/screenshot", raw=True)
            return True
        except Exception:
            return False
