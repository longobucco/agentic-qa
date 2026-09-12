"""Host-side counterpart of guest_proxy.py: runs the same fixture addon (plus drive_mock.py when
a task needs it) as a subprocess on the harness host, for the two getters that make their own
network calls from the evaluator process rather than through the sandbox's Chrome
(get_cloud_file's `requests.get`, get_googledrive_file's pydrive2 calls -- see
docs/g_astra_open_book_runner_implementation.md and the three CDP-driven getters that don't need
this at all, already covered once Chrome is proxied).

Used as a context manager scoped to exactly one `evaluate_official()` call
(runners/gpt_astra_openbook.py) via HTTP(S)_PROXY + REQUESTS_CA_BUNDLE env vars -- process-wide
for that call's duration, restored immediately after, never left set for the rest of the runner.
"""
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

_ADDON = Path(__file__).resolve().parents[1] / "openbook_proxy" / "addon.py"
_DRIVE_MOCK = Path(__file__).resolve().parents[1] / "openbook_proxy" / "drive_mock.py"
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "REQUESTS_CA_BUNDLE")


class HostProxyError(RuntimeError):
    pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port, tries=30, interval=0.5):
    for _ in range(tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(interval)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(interval)
    return False


def _ca_cert_path(confdir):
    return Path(confdir) / "mitmproxy-ca-cert.pem"


@contextmanager
def host_proxy(bundle_dir, *, needs_drive_mock=False, confdir=None, miss_log=None):
    """`confdir`: reuse the same pre-generated, pre-trusted CA the guest image bakes in (see
    docker/Dockerfile.osworld) -- a fresh per-run CA would need `REQUESTS_CA_BUNDLE` regenerated
    every run, no better than the guest-side reasoning in guest_proxy.py. Falls back to a throwaway
    confdir (mitmproxy self-signs on first run) with a printed warning: usable for local
    development, not for a frozen campaign (the lock records confdir/CA identity separately)."""
    if shutil.which("mitmdump") is None:
        raise HostProxyError("mitmdump not found on PATH -- install mitmproxy in the harness venv")
    port = _free_port()
    confdir = confdir or os.path.expanduser("~/.osw_openbook_host_mitmproxy")
    Path(confdir).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OSW_OPENBOOK_BUNDLE"] = str(bundle_dir)
    if miss_log:
        env["OSW_OPENBOOK_MISS_LOG"] = str(miss_log)
    cmd = ["mitmdump", "--mode", "regular", "--listen-host", "127.0.0.1",
          "--listen-port", str(port), "--set", f"confdir={confdir}", "-s", str(_ADDON)]
    if needs_drive_mock:
        cmd += ["-s", str(_DRIVE_MOCK)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_listening(port):
            proc.terminate()
            raise HostProxyError(f"host mitmdump never started listening on 127.0.0.1:{port}")
        proxy_url = f"http://127.0.0.1:{port}"
        ca_cert = _ca_cert_path(confdir)
        yield {"proxy_url": proxy_url, "port": port,
              "ca_cert": str(ca_cert) if ca_cert.exists() else None}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def scoped_env(handle):
    """Set HTTP(S)_PROXY/REQUESTS_CA_BUNDLE process-wide for the duration of the `with` block
    only, then restore exactly what was there before -- `requests` (get_cloud_file) and pydrive2
    (get_googledrive_file) both read these from os.environ at call time, and neither takes a
    proxy argument the evaluator getters could be handed directly (they're upstream OSWorld code,
    not ours to modify)."""
    previous = {k: os.environ.get(k) for k in _PROXY_ENV_VARS}
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = handle["proxy_url"]
    os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = handle["proxy_url"]
    if handle.get("ca_cert"):
        os.environ["REQUESTS_CA_BUNDLE"] = handle["ca_cert"]
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
