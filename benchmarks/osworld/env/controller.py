"""HTTP client to OSWorld's in-guest server (desktop_env/server/main.py).

Routes verified against upstream: GET /screenshot, GET /accessibility, POST /run_python {code},
POST /execute {command,shell}, POST /file (form file_path) -> raw bytes.
"""
import json
import urllib.parse
import urllib.request


class Controller:
    def __init__(self, base_url, *, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, *, raw=False):
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as r:
            data = r.read()
        return data if raw else data.decode()

    def _post_json(self, path, payload, *, timeout=None):
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read().decode()

    def _post_form(self, path, form, *, timeout=None):
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read()

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
