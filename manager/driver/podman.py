from io import BytesIO
import os
import tarfile
from . import Driver
import podman
import docker


class PodmanDriver(Driver):
    def __init__(self):
        self.client = podman.PodmanClient()

    def has_image(self, name):
        try:
            docker.from_env().images.get(name)
            return True
        except docker.errors.ImageNotFound:
            return False

    def build(self, name, path):
        docker.from_env().images.build(path=path, tag=name, rm=True, nocache=True)

    def pull(self, name):
        docker.from_env().images.pull(name)

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
            ports=ports or {},
            environment=env or {},
        )
        container_ip = container.network_settings["IPAddress"]
        port_mapping = container.ports
        return container_ip, port_mapping, None

    def stop(self, name):
        try:
            self.client.containers.get(name).stop()
            print(f"- stopped {name}")
        except docker.errors.NotFound:
            pass

    def download(self, name, src_path, dst_path):
        try:
            stream, _ = docker.from_env().containers.get(name).get_archive(src_path)

            fo = BytesIO()
            for d in stream:
                fo.write(d)
            fo.seek(0)
            with tarfile.open(fileobj=fo) as tar:
                tar.extractall(dst_path)

            print(f"- stored backend logs")
        except:
            print(f"- could not store backend logs")

    def peek(self, name, path):
        stream, _ = docker.from_env().containers.get(name).get_archive(path)

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
        docker.from_env().containers.get(name).put_archive(
            os.path.dirname(dst_path), fo
        )

    def cleanup(self, image_prefix=""):
        containers = []
        for container in docker.from_env().containers.list():
            if any(
                x in container.attrs["Config"]["Image"]
                for x in ("irc-server", "btc-node", "wasabi-backend", "wasabi-client", "joinmarket-client-server", "joinmarket-ng")
            ):
                containers.append(container)
                
        self.stop_many(map(lambda x: x.name, containers))
