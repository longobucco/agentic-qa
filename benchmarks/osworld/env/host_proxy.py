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
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "REQUESTS_CA_BUNDLE",
                  "NO_PROXY", "no_proxy")


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


def _combined_ca_bundle(confdir, ca_cert):
    """REQUESTS_CA_BUNDLE REPLACES requests' trust store, it doesn't add to it -- confirmed live
    2026-09-12: pointing it at our mitmproxy CA alone broke TLS verification for the REAL Daytona
    controller (a legitimately-signed, unproxied endpoint reached in the same requests session as
    a proxied external download, e.g. SetupController._download_setup's upload-back-to-guest
    call). A single bundle trusting both -- certifi's normal default plus our CA appended -- fixes
    proxied AND direct HTTPS calls in the same scoped_env block. Cached in confdir; certifi's
    bundle changes only on a dependency upgrade, so a stale copy is a rebuild away, not a
    correctness risk."""
    combined = Path(confdir) / "combined-ca-bundle.pem"
    try:
        import certifi
        system_bundle = Path(certifi.where()).read_bytes()
    except ImportError:
        system_bundle = b""
    combined.write_bytes(system_bundle + b"\n" + Path(ca_cert).read_bytes())
    return combined


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
    # connection_strategy=lazy: mitmproxy's default (eager) tries to actually connect upstream
    # during CONNECT handling to learn real certificate details -- confirmed live 2026-09-12 that
    # this produces a 502 from mitmproxy ITSELF for a genuinely non-resolvable fixture host,
    # before our addon's request() hook (which always sets flow.response) ever runs. lazy defers
    # the real connection until actually needed, which never happens here.
    cmd = ["mitmdump", "--mode", "regular", "--listen-host", "127.0.0.1",
          "--listen-port", str(port), "--set", f"confdir={confdir}",
          "--set", "connection_strategy=lazy", "-s", str(_ADDON)]
    if needs_drive_mock:
        cmd += ["-s", str(_DRIVE_MOCK)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_listening(port):
            proc.terminate()
            raise HostProxyError(f"host mitmdump never started listening on 127.0.0.1:{port}")
        proxy_url = f"http://127.0.0.1:{port}"
        ca_cert = _ca_cert_path(confdir)
        combined_ca_bundle = _combined_ca_bundle(confdir, ca_cert) if ca_cert.exists() else None
        yield {"proxy_url": proxy_url, "port": port,
              "ca_cert": str(combined_ca_bundle) if combined_ca_bundle else None}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def scoped_env(handle, *, no_proxy_hosts=()):
    """Set HTTP(S)_PROXY/REQUESTS_CA_BUNDLE process-wide for the duration of the `with` block
    only, then restore exactly what was there before -- `requests` (get_cloud_file) and pydrive2
    (get_googledrive_file) both read these from os.environ at call time, and neither takes a
    proxy argument the evaluator getters could be handed directly (they're upstream OSWorld code,
    not ours to modify).

    `no_proxy_hosts`: hostnames `requests` must reach directly, bypassing this proxy entirely.
    Confirmed live 2026-09-12: SetupController._download_setup fetches the external URL AND THEN
    uploads the result to the real Daytona controller (POST .../setup/upload) in the same
    requests session -- a blanket HTTP_PROXY caught that upload too, so the controller's own
    hostname must always be excluded whenever this wraps a call that also talks to the guest.

    pydrive2 (get_googledrive_file, _googledrive_setup) doesn't use `requests` at all -- it's
    built on httplib2, which reads http_proxy/https_proxy (already set above) but does NOT read
    REQUESTS_CA_BUNDLE or any other env var for TLS trust; it only respects the module-level
    `httplib2.CA_CERTS` path. Confirmed live 2026-09-12 with real pydrive2 1.21.3: without this,
    every pydrive2 call failed TLS verification against our mitmproxy cert even with
    REQUESTS_CA_BUNDLE correctly set. Patched and restored the same way as the env vars; a no-op
    if httplib2 isn't installed (only pulled in by pydrive2, which not every task needs)."""
    previous = {k: os.environ.get(k) for k in _PROXY_ENV_VARS}
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = handle["proxy_url"]
    os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = handle["proxy_url"]
    if handle.get("ca_cert"):
        os.environ["REQUESTS_CA_BUNDLE"] = handle["ca_cert"]
    if no_proxy_hosts:
        no_proxy_value = ",".join(no_proxy_hosts)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = no_proxy_value
    previous_ca_certs = None
    httplib2_module = None
    if handle.get("ca_cert"):
        try:
            import httplib2 as httplib2_module
            previous_ca_certs = httplib2_module.CA_CERTS
            httplib2_module.CA_CERTS = handle["ca_cert"]
        except ImportError:
            httplib2_module = None
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if httplib2_module is not None:
            httplib2_module.CA_CERTS = previous_ca_certs
