"""Start the open-book fixture proxy inside the Daytona sandbox and lock down its egress.

Built entirely on Controller.execute/pyautogui (env/controller.py) -- no new controller route,
no Daytona SDK network feature (none exists; confirmed during planning that
CreateSandboxFromImageParams takes only image/public/resources/auto_stop_interval). The guest
image is expected to have mitmproxy installed and a pre-generated, pre-trusted CA baked in at
`_GUEST_CA_DIR` (see docker/Dockerfile.osworld) -- generating a fresh CA per sandbox would need
Chrome's NSS db re-provisioned every run, which is exactly the kind of per-run drift the frozen
campaign lock is supposed to rule out.

Chrome is pointed here via OSWorld's own existing hook: SetupController.setup(steps,
use_proxy=True) appends `--proxy-server=http://127.0.0.1:18888` to any "launch" step starting
google-chrome (setup.py:309-310) -- this module owns making something real listen on that port
before that setup step runs.
"""
import base64
import json
import time
from pathlib import Path

_GUEST_DIR = "/tmp/osw_openbook"
_GUEST_CA_DIR = "/opt/osw_openbook/mitmproxy_confdir"   # baked into the image, read-only, reused
_GUEST_PORT = 18888
_MISS_LOG = f"{_GUEST_DIR}/misses.jsonl"
_PROXY_LOG = f"{_GUEST_DIR}/mitmdump.log"

_PROXY_SRC_DIR = Path(__file__).resolve().parents[1] / "openbook_proxy"

# Response to an already-established inbound connection (the OSWorld controller's own listening
# socket, which Daytona's reverse proxy reaches from outside) is OUTPUT-direction traffic too --
# without this ACCEPT the lockdown below would sever the harness's own connection to the guest,
# not just Chrome's route to the live Internet.
_IPTABLES_RULES = (
    "iptables -C OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || "
    "iptables -I OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
    "iptables -C OUTPUT -o lo -j ACCEPT 2>/dev/null || iptables -I OUTPUT -o lo -j ACCEPT",
    "iptables -C OUTPUT -j DROP 2>/dev/null || iptables -A OUTPUT -j DROP",
)


class GuestProxyError(RuntimeError):
    pass


def _write_guest_file(ctrl, remote_path, content):
    b64 = base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode()
    remote_dir = remote_path.rsplit("/", 1)[0]
    ctrl.execute(f"mkdir -p {remote_dir} && echo {b64} | base64 -d > {remote_path}", shell=True)


def _push_proxy_source(ctrl):
    """addon.py + bundle.py side by side, flat -- see addon.py's import fallback. Pushed fresh
    every run (they're tiny) rather than baked into the image, so a fix here lands on the next
    run without rebuilding/repushing the Daytona image."""
    _write_guest_file(ctrl, f"{_GUEST_DIR}/addon.py",
                      (_PROXY_SRC_DIR / "addon.py").read_bytes())
    _write_guest_file(ctrl, f"{_GUEST_DIR}/bundle.py",
                      (_PROXY_SRC_DIR / "bundle.py").read_bytes())


def _push_bundle(ctrl, bundle_dir):
    """Mirrors the local bundle directory (manifest.json + bodies/*) into the guest. There is no
    upload route on the controller (`/file` is read-only, controller.py:52-53) -- writing through
    /run_python's shell-exec channel is the only primitive available, so each file goes over as
    one base64 blob."""
    bundle_dir = Path(bundle_dir)
    manifest = bundle_dir / "manifest.json"
    _write_guest_file(ctrl, f"{_GUEST_DIR}/fixtures/manifest.json", manifest.read_bytes())
    for body in (bundle_dir / "bodies").glob("*"):
        _write_guest_file(ctrl, f"{_GUEST_DIR}/fixtures/bodies/{body.name}", body.read_bytes())


def _proxy_listening(ctrl):
    out = ctrl.execute(
        f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':{_GUEST_PORT} ' "
        f"&& echo LISTENING || echo NOT_LISTENING",
        shell=True, timeout=15,
    )
    return "LISTENING" in (out or "")


def _install_ca_into_nss(ctrl):
    """Chrome's NSS db lives at $HOME/.pki/nssdb, resolved by the GUEST's own shell -- not baked
    at image build time, for the same reason the Chrome launch wrapper resolves $HOME itself
    rather than hardcoding /root (docker/Dockerfile.osworld).

    `certutil -N --empty-password` against a db that ALREADY EXISTS is not a harmless no-op --
    confirmed live (2026-09-12): it spins forever re-prompting for a password on a closed stdin
    ("Error opening input terminal for read" / "Invalid password. Try again.", tens of MB of
    output, 100% CPU, outliving even the guest server's own subprocess timeout=120 on main.py's
    /execute route). -N is now gated on `certutil -L` (list) FAILING, i.e. only runs against a
    genuinely absent db, once per sandbox lifetime -- every guest_proxy.start() call after the
    first one in the same sandbox (OSW_SANDBOX_ID reuse) skips it via the short-circuit."""
    ca_pem = f"{_GUEST_CA_DIR}/mitmproxy-ca-cert.pem"
    ctrl.execute(
        f'mkdir -p "$HOME/.pki/nssdb" && '
        f'(certutil -L -d "sql:$HOME/.pki/nssdb" >/dev/null 2>&1 || '
        f'certutil -N -d "sql:$HOME/.pki/nssdb" --empty-password) && '
        f'certutil -D -d "sql:$HOME/.pki/nssdb" -n osw-openbook 2>/dev/null; '
        f'certutil -A -d "sql:$HOME/.pki/nssdb" -t C,, -n osw-openbook -i {ca_pem}',
        shell=True, timeout=30,
    )


def _lockdown_active(ctrl):
    out = ctrl.execute("iptables -S OUTPUT 2>/dev/null", shell=True, timeout=15) or ""
    return "-j DROP" in out


def start(ctrl, bundle_dir, *, poll_tries=15, poll_interval=1.0):
    """Push bundle + addon, start mitmdump in the background, confirm it's listening, install the
    proxy's CA into Chrome's trust store, then apply and VERIFY the egress lockdown. Raises
    GuestProxyError on any step failing -- the caller (runner) must treat this as
    ENVIRONMENT_ERROR, never run the agent against an unconfirmed proxy or an unconfirmed
    lockdown (see the plan's non-goal: never silently fall back to live Internet access).

    Every task provisions a fresh sandbox (env.sandbox's normal path), so there is ordinarily
    nothing already listening -- but confirmed live: under OSW_SANDBOX_ID reuse (an explicitly
    warned-about debug path, sandbox.py's _warn_reuse_once), a leftover mitmdump from an earlier
    call would keep answering on the OLD bundle while _proxy_listening() reports success on the
    NEW one's behalf. Killing any prior instance first makes a re-run actually pick up the bundle
    just pushed, not a stale one.

    The whole sequence runs under one try/except: confirmed live (2026-09-12) that a step here
    can raise something other than GuestProxyError (a bare urllib TimeoutError out of
    Controller.execute, on a slow certutil call) -- uncaught, that would propagate past
    env.sandbox's own except GuestProxyError and crash the provisioning worker thread instead of
    degrading to a clean ENVIRONMENT_ERROR. Every failure here is equally fatal to the run either
    way, so normalizing the exception type is the fix, not chasing each individual cause."""
    try:
        ctrl.execute(
            f"pkill -f 'mitmdump.*--listen-port {_GUEST_PORT}' 2>/dev/null; sleep 1; true",
            shell=True, timeout=15)
        ctrl.execute(f"rm -rf {_GUEST_DIR}/fixtures", shell=True, timeout=15)
        _push_proxy_source(ctrl)
        _push_bundle(ctrl, bundle_dir)
        ctrl.execute(f"mkdir -p {_GUEST_DIR}", shell=True)
        start_cmd = (
            f"cd {_GUEST_DIR} && "
            f"OSW_OPENBOOK_BUNDLE={_GUEST_DIR}/fixtures OSW_OPENBOOK_MISS_LOG={_MISS_LOG} "
            f"nohup mitmdump --mode regular --listen-port {_GUEST_PORT} "
            f"--set confdir={_GUEST_CA_DIR} -s {_GUEST_DIR}/addon.py "
            f">{_PROXY_LOG} 2>&1 & disown"
        )
        ctrl.execute(start_cmd, shell=True, timeout=20)
        for _ in range(poll_tries):
            if _proxy_listening(ctrl):
                break
            time.sleep(poll_interval)
        else:
            log_tail = ctrl.execute(f"tail -c 4000 {_PROXY_LOG} 2>/dev/null", shell=True)
            raise GuestProxyError(
                f"guest mitmdump never started listening on {_GUEST_PORT}: {log_tail!r}")
        _install_ca_into_nss(ctrl)
        for rule in _IPTABLES_RULES:
            ctrl.execute(rule, shell=True, timeout=15)
        if not _lockdown_active(ctrl):
            raise GuestProxyError(
                "iptables OUTPUT DROP rule did not take effect (sandbox may lack NET_ADMIN) -- "
                "refusing to run the agent without a confirmed egress lockdown"
            )
    except GuestProxyError:
        raise
    except Exception as e:
        raise GuestProxyError(f"open-book guest proxy setup failed: {type(e).__name__}: {e}") \
            from e


def misses(ctrl):
    """Read back whatever the guest proxy logged as unresolved (fixture_miss/bundle_unavailable)
    -- called by the runner after the agent finishes, folded into network_provenance.json so a
    silent miss never looks identical to a clean run."""
    raw = ctrl.execute(f"cat {_MISS_LOG} 2>/dev/null", shell=True, timeout=15) or ""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def egress_probe(ctrl, host="example.com", timeout=6):
    """Manual-canary helper (see plan's verification checklist): confirms the iptables lockdown
    actually blocks a NEW outbound connection attempt to a non-allowlisted host. Not called by
    the runner itself -- a live network probe on every run would be its own egress exception."""
    out = ctrl.execute(
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {timeout} http://{host}/ "
        f"|| echo BLOCKED",
        shell=True, timeout=timeout + 5,
    )
    return (out or "").strip()
