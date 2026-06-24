"""Launch/stop a local Chrome-for-Testing with a CDP debug port.

agent-browser's auto-launch daemon is unreliable in headless/automation contexts, so the
harness owns the browser lifecycle and the agent attaches with `agent-browser connect
<port>`. Each task gets a fresh user-data-dir for isolation.

Benchmark-agnostic: the caller supplies the Chrome binary path and the headless flag (a
live-site benchmark resolves these from its own config and wires them in via
`core.environment.live_site_environment`).
"""
import shutil
import subprocess
import tempfile
import time
import urllib.request


class Chrome:
    def __init__(self, port: int, chrome_bin: str, headless: bool = True):
        self.port = port
        self.chrome_bin = chrome_bin
        self.headless = headless
        self.profile = tempfile.mkdtemp(prefix=f"agentqa-chrome-{port}-")
        self.proc = None

    def start(self):
        args = [
            self.chrome_bin,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
        ]
        if self.headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        url = f"http://127.0.0.1:{self.port}/json/version"
        for _ in range(60):
            try:
                urllib.request.urlopen(url, timeout=1)
                return self
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("Chrome exited before opening the debug port")
                time.sleep(0.5)
        raise RuntimeError(f"Chrome devtools never came up on port {self.port}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
