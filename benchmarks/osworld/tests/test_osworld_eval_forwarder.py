"""End-to-end proof that scoring reaches the guest through the loopback forwarder, driving the
REAL upstream getter (desktop_env.evaluators.getters.general.get_vm_command_line) against a
fake in-guest controller on loopback:

  python -m benchmarks.osworld.tests.test_osworld_eval_forwarder

What this pins down is the plumbing the JSONDecodeError finding turned on: the getter builds
"http://{env.vm_ip}:{env.server_port}/execute" itself, so unless those two point at something
that speaks plain HTTP and forwards the POST intact, scoring cannot work. The other half --
that http://<proxy-host>:443 answers 400 text/html -- was measured live against a real Daytona
URL and is recorded in docs/finding-sonnet5-oracle-http-scheme.md; it can't be unit-tested
without a sandbox.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.osworld.env import osworld_eval

SEEN = []


class _Guest(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n)) if n else {}
        SEEN.append((self.path, payload))
        body = json.dumps({"status": "success", "output": "extension.installed\n",
                           "error": ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _guest():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Guest)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _task(expected):
    return {
        "id": "forwarder-e2e",
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line",
                       "command": ["code", "--list-extensions"]},
            "expected": {"type": "rule", "rules": {"expected": expected}},
        },
    }


def test_a_vm_command_line_evaluator_scores_through_the_forwarder():
    SEEN.clear()
    srv, url = _guest()
    try:
        reward = osworld_eval.evaluate_official(url, _task("extension.installed\n"), [])
    finally:
        srv.shutdown()
        srv.server_close()
    assert reward == 1.0, reward
    # and it really went through /execute as a POST carrying the task's own command
    assert SEEN and SEEN[0][0] == "/execute"
    assert SEEN[0][1]["command"] == ["code", "--list-extensions"]


def test_the_same_evaluator_still_says_no_when_the_output_differs():
    """A forwarder that answered everything with a 1.0 would be worse than the bug it fixes."""
    srv, url = _guest()
    try:
        reward = osworld_eval.evaluate_official(url, _task("something-else\n"), [])
    finally:
        srv.shutdown()
        srv.server_close()
    assert reward == 0.0, reward


def test_a_fail_answer_still_short_circuits_without_touching_the_guest():
    """The behaviour that made 15 Sonnet-5 FAILUREs legitimate rather than oracle artifacts:
    the agent's own FAIL is scored 0.0 before any getter runs."""
    SEEN.clear()
    srv, url = _guest()
    try:
        reward = osworld_eval.evaluate_official(url, _task("extension.installed\n"), ["FAIL"])
    finally:
        srv.shutdown()
        srv.server_close()
    assert reward == 0.0
    assert SEEN == []


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
        print("ok", name)
