import os
import tarfile
from functools import cached_property
from io import BytesIO

import docker

from . import (
    RESERVED_PORT_RANGE,
    RESERVED_PORTS_SYSCTL,
    Driver,
    managed_label_filters,
    managed_labels,
    warn_if_host_ports_unreserved,
)

BYTES_IN_MEGABYTE = 1024 * 1024


class DockerDriver(Driver):
    def __init__(self, namespace="coinjoin", run_id=None):
        self.client: docker.DockerClient = docker.from_env()
        self._namespace = namespace
        self._run_id = run_id
        warn_if_host_ports_unreserved(os.environ.get("DOCKER_HOST"))

    @cached_property
    def network(self):
        return self.client.networks.create(self._namespace, driver="bridge")

    def has_image(self, name):
        try:
            self.client.images.get(name)
            return True
        except docker.errors.ImageNotFound:
            return False

    def build(self, name, path, build_args=None):
        self.client.images.build(
            path=path, tag=name, rm=True, nocache=True, buildargs=build_args or None
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
        self.client.containers.run(
            image,
            detach=True,
            auto_remove=False,
            name=name,
            hostname=name,
            network=self.network.id,
            ports=ports or {},
            environment=env or {},
            volumes=kwargs.get("volumes"),
            command=kwargs.get("command"),
            labels=managed_labels(self._namespace, self._run_id),
            sysctls={RESERVED_PORTS_SYSCTL: RESERVED_PORT_RANGE},
        )
        return name, dict(ports or {}), None

    def container_state(self, name):
        try:
            return self.client.containers.get(name).status
        except docker.errors.NotFound:
            return "gone"
        except docker.errors.APIError:
            return None

    def stop(self, name):
        try:
            container = self.client.containers.get(name)
            container.stop()
            container.remove(force=True, v=True)
            print(f"- stopped {name}")
        except docker.errors.NotFound:
            pass

    def download(self, name, src_path, dst_path):
        container = None
        paused = False
        try:
            container = self.client.containers.get(name)
            container.reload()
            if container.status == "running":
                # Docker builds the archive while reading the live filesystem;
                # a growing log otherwise invalidates the tar stream with
                # "archive/tar: write too long".
                container.pause()
                paused = True
            stream, _ = container.get_archive(src_path)

            fo = BytesIO()
            for d in stream:
                fo.write(d)
            fo.seek(0)
            with tarfile.open(fileobj=fo) as tar:
                tar.extractall(dst_path)
        except (docker.errors.APIError, docker.errors.NotFound, tarfile.TarError, OSError) as error:
            raise RuntimeError(f"Failed to download {name}:{src_path} to {dst_path}: {error}") from error
        finally:
            if paused and container is not None:
                container.unpause()

    def pause(self, name):
        try:
            self.client.containers.get(name).pause()
        except (docker.errors.APIError, docker.errors.NotFound) as error:
            raise RuntimeError(f"Failed to pause {name}: {error}") from error

    def unpause(self, name):
        try:
            self.client.containers.get(name).unpause()
        except (docker.errors.APIError, docker.errors.NotFound) as error:
            raise RuntimeError(f"Failed to unpause {name}: {error}") from error

    def peek(self, name, path):
        stream, _ = self.client.containers.get(name).get_archive(path)

        fo = BytesIO()
        for d in stream:
            fo.write(d)
        fo.seek(0)
        with tarfile.open(fileobj=fo) as tar:
            return tar.extractfile(os.path.basename(path)).read().decode()

    def get_pod_resource_usage(self, name):
        """Memory usage of a container, mirroring the Kubernetes driver's shape."""
        try:
            container = self.client.containers.get(name)
            stats = container.stats(stream=False)
        except (docker.errors.NotFound, docker.errors.APIError, OSError):
            return None

        memory = stats.get("memory_stats") or {}
        usage = memory.get("usage")
        limit = memory.get("limit")
        if not usage or not limit:
            return None

        memory_mb = usage / BYTES_IN_MEGABYTE
        memory_limit_mb = limit / BYTES_IN_MEGABYTE
        return {
            "memory_mb": memory_mb,
            "memory_limit_mb": memory_limit_mb,
            "memory_percent": (memory_mb / memory_limit_mb) * 100 if memory_limit_mb else 0.0,
        }

    def logs(self, name):
        return self.client.containers.get(name).logs(stdout=True, stderr=True).decode(errors="replace")

    def upload(self, name, src_path, dst_path):
        fo = BytesIO()
        with tarfile.open(fileobj=fo, mode="w") as tar:
            tar.add(src_path, os.path.basename(dst_path))
        fo.seek(0)
        self.client.containers.get(name).put_archive(os.path.dirname(dst_path), fo)

    def cleanup(self, image_prefix=""):
        containers = self.client.containers.list(
            all=True,
            filters={"label": managed_label_filters(self._namespace, self._run_id)},
        )
        self.stop_many(map(lambda x: x.name, containers))
        networks = self.client.networks.list(self._namespace)
        if networks:
            for network in networks:
                network.remove()
