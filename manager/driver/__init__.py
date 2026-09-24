from abc import ABC, abstractmethod
from multiprocessing.pool import ThreadPool

MANAGED_LABEL = "coinjoin-emulator.managed"
NAMESPACE_LABEL = "coinjoin-emulator.namespace"
RUN_ID_LABEL = "coinjoin-emulator.run-id"


def managed_labels(namespace: str, run_id: str | None = None) -> dict[str, str]:
    """Return ownership labels for resources created by one emulator run."""
    labels = {
        MANAGED_LABEL: "true",
        NAMESPACE_LABEL: namespace,
    }
    if run_id:
        labels[RUN_ID_LABEL] = run_id
    return labels


def managed_label_filters(namespace: str, run_id: str | None = None) -> list[str]:
    """Return runtime label filters scoped like :func:`managed_labels`."""
    return [f"{key}={value}" for key, value in managed_labels(namespace, run_id).items()]

# The Wasabi backend, coordinator and clients bind fixed ports inside the
# default ephemeral range, where the kernel can hand the same port to an
# outgoing connection first and make the bind fail.
RESERVED_PORT_RANGE = "37127-37260"
RESERVED_PORTS_SYSCTL = "net.ipv4.ip_local_reserved_ports"
HOST_RESERVED_PORTS_PATH = "/proc/sys/net/ipv4/ip_local_reserved_ports"


def _parse_port_ranges(value: str) -> list[tuple[int, int]]:
    ranges = []
    for item in "".join(value.split()).split(","):
        if not item:
            continue
        low, _, high = item.partition("-")
        ranges.append((int(low), int(high or low)))
    return ranges


def warn_if_host_ports_unreserved(
    daemon_host: str | None = None, path: str = HOST_RESERVED_PORTS_PATH
) -> None:
    """Warn when published service ports can collide with host outgoing connections.

    Docker and Podman publish the same fixed ports on the host, where the
    container sysctl does not apply; a collision fails the client start.
    A remote daemon (e.g. the pipeline's dind) binds them in its own network
    namespace, which this process cannot inspect.
    """
    if daemon_host and not daemon_host.startswith("unix://"):
        return
    try:
        with open(path, encoding="ascii") as handle:
            existing = "".join(handle.read().split())
            reserved = _parse_port_ranges(existing)
    except (OSError, ValueError):
        return
    required_low, required_high = _parse_port_ranges(RESERVED_PORT_RANGE)[0]
    if not any(low <= required_low and required_high <= high for low, high in reserved):
        combined = f"{existing},{RESERVED_PORT_RANGE}" if existing else RESERVED_PORT_RANGE
        print(
            f"WARNING: host does not reserve ports {RESERVED_PORT_RANGE}; clients may fail with "
            f"'address already in use'. Run: sudo sysctl -w {RESERVED_PORTS_SYSCTL}={combined}"
        )


class Driver(ABC):
    @abstractmethod
    def has_image(self, name):
        pass

    @abstractmethod
    def build(self, name, path, build_args=None):
        pass

    @abstractmethod
    def pull(self, name):
        pass

    @abstractmethod
    def run(
        self,
        name,
        image,
        env=None,
        ports=None,
        cpu=None,
        memory=None,
        **kwargs
    ):
        pass

    def container_state(self, _name):  # pylint: disable=useless-return
        """Return a human-readable container state, if available."""
        return None

    @abstractmethod
    def stop(self, name):
        pass

    def stop_many(self, names):
        with ThreadPool() as p:
            p.map(lambda x: self.stop(x), names)

    @abstractmethod
    def pause(self, name):
        """Freeze every process of the container so its filesystem stops changing."""

    @abstractmethod
    def unpause(self, name):
        """Resume a container frozen by :meth:`pause`."""

    @abstractmethod
    def download(self, name, src_path, dst_path):
        pass

    @abstractmethod
    def peek(self, name, path):
        pass

    @abstractmethod
    def logs(self, name):
        """Return the container's combined stdout and stderr."""

    def upload(self, name, src_path, dst_path):
        pass

    def get_pod_resource_usage(self, name):
        """Return memory usage for a running container, or None when unknown.

        The engine samples this during a run; a driver that cannot report usage
        answers None instead of raising, so the sampling degrades to a no-op
        rather than logging a failure on every check.
        """
        return None

    @abstractmethod
    def cleanup(self, image_prefix=""):
        pass
