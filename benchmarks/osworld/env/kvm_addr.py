"""Address sanity for the kvm backend, importable without benchmarks.osworld.config (the
campaign driver checks it before exporting the protocol environment config reads at import)."""
import ipaddress


def loopback_conflict(addr, docker_host):
    """Why OSW_KVM_ADDR can't work with this OSW_KVM_DOCKER_HOST, or None. Published ports are
    random ports on the DOCKER host: a loopback address reaches them only when that host is this
    machine (a local socket, or unset)."""
    if not docker_host or docker_host.startswith("unix://"):
        return None
    host = addr.strip("[]")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        return None
    return (f"OSW_KVM_ADDR={addr} is a loopback address but OSW_KVM_DOCKER_HOST={docker_host} is "
            f"a remote docker host: the VM's ports are published on random ports of THAT host, "
            f"which {addr} does not reach. Run the harness on the docker host itself (unix "
            f"socket), or set OSW_KVM_ADDR to an address of the docker host routable from here.")
