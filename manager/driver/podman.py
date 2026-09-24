import os
import tarfile
from functools import cached_property
from io import BytesIO

import podman

from manager.exceptions import CoinjoinEmulatorError

from . import (
    RESERVED_PORT_RANGE,
    RESERVED_PORTS_SYSCTL,
    Driver,
    managed_label_filters,
    managed_labels,
    warn_if_host_ports_unreserved,
)


class PodmanDriver(Driver):
    def __init__(self, namespace="coinjoin", run_id=None):
        self._namespace = namespace
        self._run_id = run_id
        self.client = podman.PodmanClient()
        warn_if_host_ports_unreserved(os.environ.get("CONTAINER_HOST"))

    @cached_property
    def network(self) -> str:
        try:
            self.client.networks.get(self._namespace)
        except podman.errors.NotFound:
            self.client.networks.create(self._namespace)
        return self._namespace

    def has_image(self, name):
        try:
            self.client.images.get(name)
            return True
        except podman.errors.ImageNotFound:
            return False

    def build(self, name: str, path: str, build_args=None) -> None:
        self.client.images.build(
            path=path,
            tag=name,
            rm=True,
            nocache=True,
            buildargs=build_args or {},
        )

    def pull(self, name):
        self.client.images.pull(name)

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
        self._remove_container(name)
        container = self.client.containers.run(
            image,
            command=kwargs.get("command"),
            detach=True,
            name=name,
            hostname=name,
            network=self.network,
            ports={str(port): host_port for port, host_port in (ports or {}).items()},
            environment=env or {},
            volumes=kwargs.get("volumes") or {},
            labels=managed_labels(self._namespace, self._run_id),
            sysctls={RESERVED_PORTS_SYSCTL: RESERVED_PORT_RANGE},
        )
        inspect = container.inspect()
        networks = inspect.get("NetworkSettings", {}).get("Networks", {})
        container_ip = next((network.get("IPAddress") for network in networks.values()), None)
        if not container_ip:
            raise CoinjoinEmulatorError(f"Podman container {name} has no network address")
        return container_ip, dict(ports or {}), None

    def stop(self, name):
        try:
            container = self.client.containers.get(name)
            container.stop(ignore=True)
            container.remove(force=True)
            print(f"- stopped {name}")
        except podman.errors.NotFound:
            pass

    def _remove_container(self, name):
        try:
            container = self.client.containers.get(name)
        except podman.errors.NotFound:
            return
        inspect = container.inspect()
        labels = inspect.get("Config", {}).get("Labels", {})
        expected = managed_labels(self._namespace, self._run_id)
        if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected.items()):
            raise CoinjoinEmulatorError(
                f"Refusing to replace container {name}: it is not owned by "
                f"namespace {self._namespace!r} and run {self._run_id!r}"
            )
        container.remove(force=True)

    def download(self, name, src_path, dst_path):
        try:
            stream, _ = self.client.containers.get(name).get_archive(src_path)

            fo = BytesIO()
            for d in stream:
                fo.write(d)
            fo.seek(0)
            with tarfile.open(fileobj=fo) as tar:
                tar.extractall(dst_path)

            print("- stored backend logs")
        except (podman.errors.PodmanError, OSError, tarfile.TarError) as error:
            raise CoinjoinEmulatorError(
                f"Failed to copy {name}:{src_path} to {dst_path}: {error}"
            ) from error

    def pause(self, name):
        try:
            self.client.containers.get(name).pause()
        except podman.errors.PodmanError as error:
            raise CoinjoinEmulatorError(f"Failed to pause {name}: {error}") from error

    def unpause(self, name):
        try:
            self.client.containers.get(name).unpause()
        except podman.errors.PodmanError as error:
            raise CoinjoinEmulatorError(f"Failed to unpause {name}: {error}") from error

    def peek(self, name, path):
        stream, _ = self.client.containers.get(name).get_archive(path)

        fo = BytesIO()
        for d in stream:
            fo.write(d)
        fo.seek(0)
        with tarfile.open(fileobj=fo) as tar:
            return tar.extractfile(os.path.basename(path)).read().decode()

    def logs(self, name: str) -> str:
        return self.client.containers.get(name).logs(
            stdout=True,
            stderr=True,
        ).decode(errors="replace")

    def upload(self, name, src_path, dst_path):
        fo = BytesIO()
        with tarfile.open(fileobj=fo, mode="w") as tar:
            tar.add(src_path, os.path.basename(dst_path))
        fo.seek(0)
        if not self.client.containers.get(name).put_archive(
            os.path.dirname(dst_path), fo.read()
        ):
            raise CoinjoinEmulatorError(f"Failed to copy {src_path} to {name}:{dst_path}")

    def cleanup(self, image_prefix=""):
        containers = self.client.containers.list(
            all=True,
            filters={"label": managed_label_filters(self._namespace, self._run_id)},
        )
        self.stop_many(map(lambda x: x.name, containers))
        try:
            self.client.networks.get(self._namespace).remove()
        except podman.errors.NotFound:
            pass
