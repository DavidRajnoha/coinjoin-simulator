import base64
import tempfile
import threading
import traceback
from functools import cached_property
from io import BytesIO
import os
import tarfile
from time import sleep
from . import Driver
from kubernetes import client, config
from kubernetes.stream import stream
from kubernetes.client.exceptions import ApiException
import backoff


class KubernetesDriver(Driver):
    def __init__(self, namespace="coinjoin", reuse_namespace=False, pull_secret_path=None, in_cluster=False):

        if in_cluster:
            try:
                config.load_incluster_config()
            except Exception as e:
                config.load_kube_config()
        else:
            config.load_kube_config()

        self.client = client.CoreV1Api()
        # kubernetes.stream.stream() swaps ApiClient.request for a websocket transport and
        # restores it afterwards. Concurrent exec calls (log gathering runs in a ThreadPool)
        # interleave those save/restore pairs, which can leave the websocket transport
        # installed permanently and break every later REST call - notably the pod listing in
        # cleanup(). Keep exec traffic on its own per-thread client so self.client stays REST.
        self._stream_clients = threading.local()
        self._namespace = namespace
        self.reuse_namespace = reuse_namespace
        self.pull_secret_path = pull_secret_path
        self.in_cluster = in_cluster

    def _stream_api(self):
        """Return a CoreV1Api dedicated to this thread's exec/stream calls."""
        api = getattr(self._stream_clients, "api", None)
        if api is None:
            api = client.CoreV1Api(client.ApiClient())
            self._stream_clients.api = api
        return api

    def _create_image_pull_secret(self):
        secret_name = "regcred"
        try:
            with open(self.pull_secret_path, "r") as f:
                dockerconfigjson = f.read()
            dockerconfigjson_b64 = base64.b64encode(dockerconfigjson.encode("utf-8")).decode("utf-8")

            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name),
                data={
                    ".dockerconfigjson": dockerconfigjson_b64
                },
                type="kubernetes.io/dockerconfigjson",
            )
            # Try to create, if exists, replace
            try:
                self.client.create_namespaced_secret(namespace=self._namespace, body=secret)
                print(f"Created image pull secret {secret_name}")
            except ApiException as e:
                if e.status == 409:  # Already exists
                    self.client.replace_namespaced_secret(secret_name, self._namespace, secret)
                    print(f"Replaced image pull secret {secret_name}")
                else:
                    raise
        except Exception as e:
            print(f"Failed to create image pull secret: {e}")

    def create_namespace(self):
        namespace_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": self._namespace},
        }
        self.client.create_namespace(body=namespace_manifest)

        @backoff.on_exception(backoff.constant, Exception, interval=5, max_time=30)
        def wait_for_active():
            ns = self.client.read_namespace(self._namespace)
            if ns.status.phase != "Active":
                print(f"Namespace '{self._namespace}' is not Active yet.")
                raise Exception(f"Namespace '{self._namespace}' not Active yet.")
            print(f"Namespace '{self._namespace}' is Active.")

        wait_for_active()

    @cached_property
    def namespace(self):
        if not self.reuse_namespace:
            self.create_namespace()
            if self.pull_secret_path:
                self._create_image_pull_secret()
        return self._namespace

    def has_image(self, name):
        return True

    def build(self, name, path):
        pass

    def pull(self, name):
        pass

    def build_pod_manifest(self, name, image, env, ports, cpu, memory,
                            user_id=None):
        if ports is None:
            ports = {}
        if env is None:
            env = {}

        security_context = {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        } if user_id is None else {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                            "runAsUser": user_id,
                            "runAsGroup": user_id,
                        }

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "image": image,
                        "imagePullPolicy": "Always",
                        "name": name,
                        "ports": [
                            {"containerPort": container_port}
                            for container_port in ports.keys()
                        ],
                        "env": [
                            {"name": k, "value": v}
                            for k, v in env.items()
                        ],
                        "securityContext": security_context,
                        "resources": {
                            "limits": {"cpu": cpu*1.5, "memory": f"{memory*1.5}Mi"},
                            "requests": {"cpu": cpu, "memory": f"{memory}Mi"},
                        },
                    }
                ],
                # Add imagePullSecrets if pull_secret_path is set
                **({"imagePullSecrets": [{"name": "regcred"}]} if self.pull_secret_path else {}),
            },
        }

    def run(
        self,
        name,
        image,
        env=None,
        ports=None,
        cpu=None,
        memory=None,
        run_as_user=None,
        **kwargs
    ):
        pod_manifest = self.build_pod_manifest(name, image, env, ports, cpu, memory, run_as_user)
        resp = self.client.create_namespaced_pod(body=pod_manifest, namespace=self.namespace)

        pod_ip = None
        try:
            while pod_ip is None:
                pod_ip = self.client.read_namespaced_pod_status(
                    name=name, namespace=self.namespace
                ).status.pod_ip
                sleep(1)
        except Exception as e:
            print(f"Failed to get pod IP: {e}")
            raise

        service_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{name}"},
            "spec": {
                "type": "NodePort",
                "selector": {"app": name},
                "ports": [
                    {
                        "name": f"{name}-{container_port}",
                        "protocol": "TCP",
                        "port": container_port,
                        "targetPort": target_port,
                    }
                    for (target_port, container_port) in ports.items()
                ],
            },
        }
        try:
            resp = self.client.create_namespaced_service(
                body=service_manifest, namespace=self.namespace
            )
        except Exception as e:
            print(f"Failed to create service: {e}")
            raise

        if self.in_cluster:
            # For in-cluster: return service DNS name, original port mapping, no route
            port_mapping = {target_port: container_port for target_port, container_port in ports.items()}
            service_dns_name = f"{name}.{self.namespace}.svc.cluster.local"
            return service_dns_name, port_mapping, None
        else:
            # For external: return pod IP, node port mapping, no route (existing behavior)
            port_mapping = dict(
                map(lambda x: (x.target_port, x.node_port), resp.spec.ports)
            )
            return pod_ip or "", port_mapping, None

    def stop(self, name):
        try:
            self.client.delete_namespaced_pod(name=name, namespace=self.namespace)
            self.client.delete_namespaced_service(
                name, namespace=self.namespace
            )
        except:
            pass

    def download(self, name, src_path, dst_path):
        if src_path[-1] == "/":
            src_path = src_path[:-1]
        src_parent, src_target = os.path.split(src_path)
        # Use rsync-like approach with tar to handle files being written to
        # The --warning=no-file-changed flag helps handle files that change during reading
        # The --ignore-failed-read flag ensures the process continues even if some files can't be read
        exec_command = [
            "tar", "cf", "-",
            "--warning=no-file-changed",
            "--ignore-failed-read",
            "-C", src_parent, src_target
        ]
        resp = stream(
            self._stream_api().connect_get_namespaced_pod_exec,
            name,
            self.namespace,
            command=exec_command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        print("Opening connection")

        # Spool the archive to disk rather than memory: a BytesIO here holds the pod's whole
        # data directory, and log gathering downloads several pods at once, so peak usage was
        # workers x archive size. That OOMKilled the manager during a 136-client teardown.
        with tempfile.NamedTemporaryFile(suffix=".tar") as fo:
            # tar warns on stderr about every file it skipped (--ignore-failed-read) or that
            # changed mid-read. Print those warnings: discarding the stream would make any such
            # loss invisible in the manager log.
            warnings = []
            try:
                while resp.is_open():
                    print("Updating stream")
                    resp.update(timeout=10)
                    if resp.peek_stdout():
                        fo.write(resp.read_stdout().encode())
                    if resp.peek_stderr():
                        warnings.append(resp.read_stderr())
            finally:
                # A truncated tar (files changing mid-read) makes the extract below raise;
                # closing here keeps that from leaking the websocket connection.
                print("Closing connection")
                resp.close()
            print("")
            if warnings:
                for line in "".join(warnings).splitlines():
                    if line.strip():
                        print(f"!! tar warning for {name}:{src_path}: {line.strip()}")
            fo.flush()
            fo.seek(0)

            with tarfile.open(fileobj=fo) as tar:
                print("Extracting")
                tar.extractall(dst_path)

        # Wait for required files to appear in dst_path
        import glob
        import time
        start_time = time.time()
        timeout = 120  # 2 minutes
        found = False
        waited = False
        while True:
            tumble_log = os.path.exists(os.path.join(dst_path, "logs/TUMBLE.log"))
            tumble_schedule = os.path.exists(os.path.join(dst_path, "logs/TUMBLE.schedule*"))
            j_logs = glob.glob(os.path.join(dst_path, "logs/J*.log"))
            yifen = os.path.exists(os.path.join(dst_path, "logs/yigen-statement.csv"))

            cond1 = tumble_log and tumble_schedule and len(j_logs) > 0
            cond2 = len(j_logs) > 0 and yifen

            print(f"Debug: tumble_log={tumble_log}, tumble_schedule={tumble_schedule}, j_logs={j_logs}, yigen={yifen}")

            if cond1 or cond2:
                print(f"All required log files found in {dst_path} after {time.time() - start_time} seconds")
                print(f"Waiting for file transfer to complete...")
                time.sleep(10)
                if found:
                    print("All required log files still found in {} after {} seconds".format(dst_path, time.time() - start_time))
                    time.sleep(1)
                    break
                found = True

            if time.time() - start_time > timeout:
                print("Timeout waiting for required log files in {}".format(dst_path))
                break

            if not waited:
                print("Waiting for required log files to appear in {}...".format(dst_path))
                waited = True
            time.sleep(2)

        # sleep(60)

    def peek(self, name, path):
        exec_command = ["cat", path]
        resp = stream(
            self._stream_api().connect_get_namespaced_pod_exec,
            name,
            self.namespace,
            command=exec_command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

        output = ""
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                output += resp.read_stdout()
        resp.close()
        return output

    def get_pod_resource_usage(self, name):
        """
        Get memory usage of a pod by reading /proc/self/status.
        Returns dict with memory_mb and memory_limit_mb, or None if failed.
        """
        try:
            # Read process memory info from /proc
            exec_command = ["cat", "/proc/self/status"]
            resp = stream(
                self._stream_api().connect_get_namespaced_pod_exec,
                name,
                self.namespace,
                command=exec_command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            output = ""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    output += resp.read_stdout()
            resp.close()

            # Parse VmRSS (Resident Set Size - actual RAM used)
            memory_kb = None
            for line in output.split('\n'):
                if line.startswith('VmRSS:'):
                    # Format: "VmRSS:      123456 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        memory_kb = int(parts[1])
                        break

            if memory_kb is None:
                return None

            # Get pod spec to find memory limit
            pod = self.client.read_namespaced_pod(name=name, namespace=self.namespace)
            memory_limit_str = pod.spec.containers[0].resources.limits.get('memory', '0Mi')
            # Parse memory limit (e.g., "128Mi" -> 128)
            memory_limit_mb = int(memory_limit_str.replace('Mi', '').replace('Gi', '000'))

            return {
                'memory_mb': memory_kb / 1024,
                'memory_limit_mb': memory_limit_mb,
                'memory_percent': (memory_kb / 1024 / memory_limit_mb * 100) if memory_limit_mb > 0 else 0
            }
        except Exception as e:
            # Silently fail - pod might be terminating
            return None

    def upload(self, name, src_path, dst_path):
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:tar") as tar:
            tar.add(src_path, arcname=dst_path)
        commands = [buf.getvalue()]

        exec_command = ["tar", "xf", "-", "-C", "/"]
        resp = stream(
            self._stream_api().connect_get_namespaced_pod_exec,
            name,
            self.namespace,
            command=exec_command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                print(f"STDOUT: {resp.read_stdout()}")
            if resp.peek_stderr():
                print(f"STDERR: {resp.read_stderr()}")
            if commands:
                c = commands.pop(0)
                resp.write_stdin(c)
            else:
                break
        resp.close()


    def _list_with_retry(self, list_method_name):
        """List namespaced resources, rebuilding the API client once on failure.

        Exec traffic runs on separate clients (see _stream_api), so self.client should
        stay usable. This retry is the safety net: a failure here used to abandon the
        whole cleanup and leave every pod of a large run holding the namespace quota.
        """
        for attempt in (1, 2):
            try:
                return getattr(self.client, list_method_name)(namespace=self._namespace)
            except ApiException as e:
                print(f"Error listing ({list_method_name}, attempt {attempt}):", e)
                traceback.print_exc()
                if attempt == 1:
                    print("Retrying with a fresh API client")
                    self.client = client.CoreV1Api(client.ApiClient())
        return None

    def cleanup(self, image_prefix=""):
        pods = self._list_with_retry("list_namespaced_pod")
        if pods is None:
            print("Cleanup failed: could not list pods; "
                  f"delete leftovers manually in namespace {self._namespace}")
            return

        for pod in pods.items:
            if any(
                    x in pod.metadata.name
                    for x in ("irc-server", "btc-node", "wasabi-backend", "wasabi-client", "joinmarket-client-server",
                              "joinmarket-distributor", "jcs", "joinmarket-obwatch")
            ):
                try:
                    print(f"Deleting pod {pod.metadata.name}")
                    self.client.delete_namespaced_pod(
                        name=pod.metadata.name, namespace=self._namespace
                    )
                    print(f"Deleted pod {pod.metadata.name}")
                except ApiException:
                    pass
        services = self._list_with_retry("list_namespaced_service")
        if services is None:
            print("Cleanup incomplete: could not list services; "
                  f"delete leftovers manually in namespace {self._namespace}")
            return
        for service in services.items:
            if any(
                    x in service.metadata.name
                    for x in ("irc-server", "btc-node", "wasabi-backend", "wasabi-client", "joinmarket-client-server",
                              "joinmarket-distributor", "jcs", "joinmarket-obwatch")
            ):
                try:
                    print("Deleting service", service.metadata.name)
                    self.client.delete_namespaced_service(
                        name=service.metadata.name, namespace=self._namespace
                    )
                    print("Deleted service", service.metadata.name)
                except ApiException:
                    pass

        if not self.reuse_namespace:
            try:
                print(f"Deleting namespace {self._namespace}")
                self.client.delete_namespace(
                    name=self._namespace, body=client.V1DeleteOptions()
                )
            except ApiException:
                pass
