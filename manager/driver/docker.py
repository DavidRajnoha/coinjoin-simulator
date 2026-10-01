from functools import cached_property
from io import BytesIO
import os
import tarfile
from . import Driver
import docker


class DockerDriver(Driver):
    def __init__(self, namespace="coinjoin"):
        self.client = docker.from_env()
        self._namespace = namespace

    @cached_property
    def network(self):
        return self.client.networks.create(self._namespace, driver="bridge")

    def has_image(self, name):
        try:
            self.client.images.get(name)
            return True
        except docker.errors.ImageNotFound:
            return False

    def build(self, name, path):
        self.client.images.build(path=path, tag=name, rm=True, nocache=True)

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
        container = self.client.containers.run(
            image,
            detach=True,
            auto_remove=True,
            name=name,
            hostname=name,
            network=self.network.id,
            ports=ports or {},
            environment=env or {},
        )
        container_ip = container.attrs['NetworkSettings']['IPAddress']
        
        # Normalize port mapping to match Kubernetes format
        # Docker format: {'8080/tcp': [{'HostIp': '', 'HostPort': '8080'}]}
        # Kubernetes format: {8080: 8080}
        raw_port_mapping = container.attrs['NetworkSettings']['Ports']
        port_mapping = {}
        
        if ports:
            for internal_port in ports.keys():
                # For Docker networking, internal container port maps to itself
                port_mapping[internal_port] = internal_port
        
        return container_ip, port_mapping, None

    def stop(self, name):
        try:
            self.client.containers.get(name).stop()
            print(f"- stopped {name}")
        except docker.errors.NotFound:
            pass

    def download(self, name, src_path, dst_path):
        try:
            stream, _ = self.client.containers.get(name).get_archive(src_path)

            fo = BytesIO()
            for d in stream:
                fo.write(d)
            fo.seek(0)
            with tarfile.open(fileobj=fo) as tar:
                tar.extractall(dst_path)
        except:
            pass

    def peek(self, name, path):
        stream, _ = self.client.containers.get(name).get_archive(path)

        fo = BytesIO()
        for d in stream:
            fo.write(d)
        fo.seek(0)
        with tarfile.open(fileobj=fo) as tar:
            return tar.extractfile(os.path.basename(path)).read().decode()

    def upload(self, name, src_path, dst_path):
        fo = BytesIO()
        with tarfile.open(fileobj=fo, mode="w") as tar:
            tar.add(src_path, os.path.basename(dst_path))
        fo.seek(0)
        self.client.containers.get(name).put_archive(os.path.dirname(dst_path), fo)

    def cleanup(self, image_prefix=""):
        
        containers = []
        for container in self.client.containers.list():
            if any(
                x in container.attrs["Config"]["Image"]
                for x in ("irc-server", "btc-node", "wasabi-backend", "wasabi-client", "joinmarket-client-server", "joinmarket-ng")
            ):
                containers.append(container)

        self.stop_many(map(lambda x: x.name, containers))
        networks = self.client.networks.list(self._namespace)
        if networks:
            for network in networks:
                network.remove()
