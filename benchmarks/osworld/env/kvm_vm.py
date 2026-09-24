"""kvm backend (config.BACKEND == "kvm"): the official OSWorld VM, started the way upstream's
Docker provider does it (desktop_env/providers/docker/provider.py @091f5ef): the
happysixd/osworld-docker container runs the official Ubuntu.qcow2 (read-only bind; the
container's overlay is thrown away with it, which is upstream's snapshot revert) with 4 CPU,
4 GB RAM, /dev/kvm, NET_ADMIN and ports 5000/9222/8080/8006 published. Host ports are left to
Docker (random), because upstream's local psutil port scan is wrong for a remote DOCKER_HOST.
Flow per upstream DesktopEnv.reset: wait for /screenshot, setup (up to 5 attempts on a False
return), then the runner waits POST_SETUP_WAIT_S.

Every consumer reaches the guest at KVM_ADDR + the mapped port: the controller (5000), the
SetupController and the evaluator adapter (9222 for CDP, 8080 for VLC). No CdpForwarder here:
unlike Daytona, the published CDP port is directly routable."""
from contextlib import contextmanager
import os
import time

import requests

from benchmarks.osworld import config
from benchmarks.osworld.env.controller import Controller
from benchmarks.osworld.env.kvm_addr import loopback_conflict   # noqa: F401 (re-export)
from core.environment import Env

_GUEST_PORTS = (5000, 9222, 8080, 8006)
_READY_TIMEOUT_S = 300
_SETUP_ATTEMPTS = 5   # desktop_env.py MAX_RETRIES
DRIVER_RUN_LABEL = "osworld.driver_run"


class KvmSetupError(RuntimeError):
    """The official VM never became ready, or its task setup failed: an infrastructure failure,
    not a task outcome. Raised out of kvm_environment (never yielded as setup_error, which the
    runners score as a terminal ENVIRONMENT_ERROR), so core.run's work() records it in
    infra_error.json under `infra_outcome` and a resumed campaign retries the run; the campaign
    driver's stuck/systemic rules count it."""
    infra_outcome = "ENV_SETUP_FAILED"


def _qcow2_mount(target):
    """Read-only bind of the qcow2, as upstream's volumes={path: {"mode": "ro"}} -- but as an
    explicit Mount, which the daemon refuses when the host path is missing (the legacy volumes
    bind silently creates it as an empty directory). abspath as upstream does: a relative path
    would otherwise be taken for a named volume."""
    from docker.types import Mount
    return Mount(target=target, source=os.path.abspath(config.KVM_QCOW2), type="bind",
                 read_only=True)


def _docker_client():
    import docker
    return docker.DockerClient(base_url=config.KVM_DOCKER_HOST)


def published_ports(container):
    """{guest port: host port} for the container's published ports."""
    container.reload()
    out = {}
    for key, binds in (container.attrs["NetworkSettings"]["Ports"] or {}).items():
        if binds:
            out[int(key.split("/")[0])] = int(binds[0]["HostPort"])
    return out


def _wait_ready(base_url, timeout=_READY_TIMEOUT_S):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(f"{base_url}/screenshot", timeout=(10, 10)).status_code == 200:
                return None
        except requests.RequestException:
            pass
        time.sleep(1)
    return f"official VM not ready after {timeout}s ({base_url}/screenshot)"


def _configure(ctrl, task):
    from benchmarks.osworld.env.sandbox import _run_config
    return _run_config(ctrl, task, enable_cdp_forwarder=False, setup_attempts=_SETUP_ATTEMPTS,
                       verify_launches=False)


@contextmanager
def kvm_environment(task, *, port=None, client=None):
    """One fresh official VM per run. Whatever raises (setup, agent, scoring, Ctrl-C), the
    container is stopped and removed -- remove runs even when stop itself raises. A process
    killed outright never reaches that finally, so the container is also labelled with the
    campaign driver's run id (OSW_KVM_DRIVER_RUN, "" outside a driver): the driver removes its
    own labelled containers after terminating its children (scripts/g_official361_driver.py)."""
    client = client or _docker_client()
    container = client.containers.run(
        config.KVM_IMAGE,
        environment={"DISK_SIZE": "32G", "RAM_SIZE": "4G", "CPU_CORES": "4"},
        cap_add=["NET_ADMIN"], devices=["/dev/kvm"],
        mounts=[_qcow2_mount("/System.qcow2")],
        ports={p: None for p in _GUEST_PORTS}, detach=True,
        labels={DRIVER_RUN_LABEL: os.environ.get("OSW_KVM_DRIVER_RUN", ""),
                "osworld.task": task["id"]})
    try:
        ports = published_ports(container)
        ctrl = Controller(f"http://{config.KVM_ADDR}:{ports[5000]}")
        ctrl.chromium_port, ctrl.vlc_port = ports[9222], ports[8080]
        ctrl.client_password = config.KVM_CLIENT_PASSWORD
        ctrl.container_id = container.id
        err = _wait_ready(ctrl.base_url) or _configure(ctrl, task)
        if err:
            raise KvmSetupError(err)
        yield Env(port=None, browser=ctrl, setup_error=None)
    finally:
        try:
            container.stop()
        finally:
            container.remove(v=True)


def preflight(client=None):
    """Refuse a campaign whose host can't run the official VM, with one SystemExit naming the
    specific problem. Cheap checks first (sha256 set, a routable address, daemon reachable, image
    present), then throwaway probe containers on the image itself, so /dev/kvm and qcow2 failures
    stay apart, and last the qcow2's content hash against OSW_KVM_QCOW2_SHA256 -- minutes on a
    ~25 GB image, so a campaign driver child skips only that one when the driver already ran it
    for this driver run (common.official_probe_already_passed)."""
    from docker.errors import APIError, ContainerError, DockerException, ImageNotFound
    from benchmarks.osworld.runners.common import official_probe_already_passed
    if not config.KVM_QCOW2_SHA256:
        raise SystemExit("OSW_KVM_QCOW2_SHA256 not set (scripts/kvm_host_setup.sh prints it)")
    conflict = loopback_conflict(config.KVM_ADDR, config.KVM_DOCKER_HOST)
    if conflict:
        raise SystemExit(f"kvm preflight: {conflict}")
    try:
        client = client or _docker_client()
        client.ping()
    except DockerException as e:
        raise SystemExit(f"kvm preflight: docker daemon unreachable at "
                         f"{config.KVM_DOCKER_HOST} ({e})")
    try:
        client.images.get(config.KVM_IMAGE)
    except ImageNotFound:
        raise SystemExit(f"kvm preflight: image {config.KVM_IMAGE} not found on the docker host "
                         f"(run scripts/kvm_host_setup.sh, or docker pull {config.KVM_IMAGE})")

    def probe(check, **kw):
        return client.containers.run(config.KVM_IMAGE, entrypoint=["sh", "-c", check],
                                     remove=True, **kw)

    try:
        probe("test -e /dev/kvm", devices=["/dev/kvm"])
    except (APIError, ContainerError) as e:
        raise SystemExit(f"kvm preflight: /dev/kvm missing or unusable on the docker host ({e})")
    qcow2 = os.path.abspath(config.KVM_QCOW2)
    try:
        probe("test -f /q && test -s /q", mounts=[_qcow2_mount("/q")])
    except ContainerError:
        raise SystemExit(f"kvm preflight: qcow2 at {qcow2} is not a regular non-empty file")
    except APIError as e:
        raise SystemExit(f"kvm preflight: qcow2 missing on the docker host at {qcow2} ({e})")
    if official_probe_already_passed():
        return
    try:
        out = probe("sha256sum /q", mounts=[_qcow2_mount("/q")])
    except (APIError, ContainerError) as e:
        raise SystemExit(f"kvm preflight: could not sha256 the qcow2 at {qcow2} ({e})")
    text = out.decode(errors="replace") if isinstance(out, bytes) else str(out or "")
    found = text.split()[0].lower() if text.split() else ""
    if found != config.KVM_QCOW2_SHA256.lower():
        raise SystemExit(f"kvm preflight: qcow2 at {qcow2} has sha256 {found or '(none)'}, but "
                         f"OSW_KVM_QCOW2_SHA256 pins {config.KVM_QCOW2_SHA256}")
