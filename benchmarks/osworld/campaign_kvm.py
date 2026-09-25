"""KVM backend of the official361 campaign driver (benchmarks/osworld/campaign.py): one official
OSWorld VM per unit, in a docker container on OSW_KVM_DOCKER_HOST.

The OSW_KVM_* host settings (address, docker host, qcow2 path/sha256) come from the caller's
environment; the kvm preflight checks they are set/present, not their identity, so they are not on
the refuse list. The guest account password upstream's system prompt tells the agent (and setup
uses), OSW_KVM_CLIENT_PASSWORD, is a harness knob pinned at its default.

The VM image is part of the protocol: OSW_KVM_IMAGE must be a digest reference of the official
image, happysixd/osworld-docker@sha256:<64 hex> (as scripts/kvm_host_setup.sh prints it), or the
driver refuses -- the kvm preflight only checks that the image EXISTS on the docker host, not
which one it is, and the system name (..._official_kvm) would not show a swap.

The caller's OSW_KVM_ADDR must be routable to OSW_KVM_DOCKER_HOST: a loopback address with a
remote docker host is refused (exit 2) -- the VM's ports are published on random ports of the
docker host, which a loopback address on this machine does not reach.

Why the container sweep exists: a run.py child the driver SIGKILLs past its grace period never
reaches kvm_environment's finally (Python's default SIGTERM action runs none, and the VM lives in
a worker thread a KeyboardInterrupt never reaches), so its container would outlive the campaign.
kvm_environment labels every container osworld.driver_run=<OSW_DRIVER_RUN>, and after every round,
after terminating the children, and on every exit the driver force-removes (with volumes) the
containers on OSW_KVM_DOCKER_HOST carrying exactly its own label. Containers with another or no
label (e.g. the other arm's campaign on the same host) are never touched: the label is re-checked
here, not only trusted to the daemon's filter.
"""
import re

NAME = "kvm"
LOG_SUFFIX = ""   # the kvm campaign keeps its log name: scripts/g_official361_<arm>.log
DRIVER_RUN_LABEL = "osworld.driver_run"   # == kvm_vm.DRIVER_RUN_LABEL (not imported: config)

# config.py's defaults for the host settings, needed before config may be imported.
_KVM_ADDR_DEFAULT = "127.0.0.1"
_KVM_DOCKER_HOST_DEFAULT = "unix:///var/run/docker.sock"

# The official VM image, pinned by digest (scripts/kvm_host_setup.sh prints the reference).
_KVM_IMAGE_RE = re.compile(r"happysixd/osworld-docker@sha256:[0-9a-f]{64}")


def protocol_env():
    return {"OSW_BACKEND": "kvm"}


def harness_knob_defaults():
    # The guest account password upstream's system prompt tells the agent (and setup uses).
    return {"OSW_KVM_CLIENT_PASSWORD": "password"}


def env_conflicts(environ):
    """The kvm-specific refusals: an unpinned/unofficial OSW_KVM_IMAGE, and a loopback
    OSW_KVM_ADDR with a remote OSW_KVM_DOCKER_HOST."""
    out = []
    image = environ.get("OSW_KVM_IMAGE")
    if not _KVM_IMAGE_RE.fullmatch((image or "").strip()):
        out.append(f"OSW_KVM_IMAGE={image!r} (the protocol requires a pinned digest "
                   f"happysixd/osworld-docker@sha256:<64 hex>; scripts/kvm_host_setup.sh "
                   f"prints it)")
    from benchmarks.osworld.env.kvm_addr import loopback_conflict   # imports no config
    loopback = loopback_conflict(
        (environ.get("OSW_KVM_ADDR") or "").strip() or _KVM_ADDR_DEFAULT,
        (environ.get("OSW_KVM_DOCKER_HOST") or "").strip() or _KVM_DOCKER_HOST_DEFAULT)
    if loopback:
        out.append(loopback)
    return out


def _docker_client():
    from benchmarks.osworld.env import kvm_vm
    return kvm_vm._docker_client()


def sweep(run_id, log, client=None):
    """Force-remove (with volumes) every container labelled osworld.driver_run == `run_id`;
    return how many were removed. The label is re-checked here, not only trusted to the daemon's
    filter: another driver's containers must never be touched. Docker errors are logged, never
    raised (this runs on the way out, possibly under a signal exit code)."""
    if not run_id:
        raise ValueError("empty driver run id: would match every container started outside a "
                         "driver")
    removed = 0
    try:
        client = client or _docker_client()
        found = client.containers.list(all=True,
                                       filters={"label": f"{DRIVER_RUN_LABEL}={run_id}"})
        for c in found:
            if (c.labels or {}).get(DRIVER_RUN_LABEL) != run_id:
                continue
            try:
                c.remove(force=True, v=True)
                removed += 1
            except Exception as e:
                log(f"container sweep: could not remove {c.id}: {e!r}")
        log(f"container sweep: removed {removed} container(s) labelled "
            f"{DRIVER_RUN_LABEL}={run_id}")
    except Exception as e:
        log(f"container sweep failed ({removed} removed): {e!r}")
    return removed
