import base64
import os
import re
import shlex
import tarfile
import time
import traceback
import uuid
from functools import cached_property
from io import BytesIO
from threading import RLock
from time import sleep

import backoff
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from manager.exceptions import KubernetesResourceQuotaError, StartupError

from . import RESERVED_PORT_RANGE, RESERVED_PORTS_SYSCTL, Driver

POD_IP_WAIT_TIMEOUT_SECONDS = int(os.environ.get("COINJOIN_K8S_POD_IP_TIMEOUT", "1800"))
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("COINJOIN_K8S_DOWNLOAD_TIMEOUT", "1800"))
UPLOAD_TIMEOUT_SECONDS = int(os.environ.get("COINJOIN_K8S_UPLOAD_TIMEOUT", "120"))
STOP_WAIT_TIMEOUT_SECONDS = int(os.environ.get("COINJOIN_K8S_STOP_TIMEOUT", "120"))
UPLOAD_COMMAND_CHUNK_SIZE = 16 * 1024
BENIGN_TAR_WARNING_RE = re.compile(
    r"^tar: .*: (file changed as we read it|socket ignored)$"
    r"|^tar: Removing leading [`'\"]?/[`'\"]? from (member names|hard link targets)$"
)


def _check_tar_stderr(name, src_path, stderr):
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not BENIGN_TAR_WARNING_RE.fullmatch(line):
            raise RuntimeError(f"download of {name}:{src_path} failed: {line}")
        print(f"[WARNING] {name}:{src_path}: {line}")


MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "coinjoin-emulator"
RUN_ID_LABEL = "coinjoin.run-id"


def _strip_reserved_ports_sysctl(pod_manifest):
    """Remove the reserved-port sysctl when the API server does not allow it."""
    spec = pod_manifest.get("spec") or {}
    security_context = spec.get("securityContext") or {}
    sysctls = security_context.get("sysctls") or []
    remaining = [sysctl for sysctl in sysctls if sysctl.get("name") != RESERVED_PORTS_SYSCTL]
    if len(remaining) == len(sysctls):
        return False
    if remaining:
        security_context["sysctls"] = remaining
    else:
        spec.pop("securityContext", None)
    return True


def _is_sysctl_rejection(error):
    return getattr(error, "status", None) in {400, 403, 422} and "sysctl" in str(
        getattr(error, "body", "") or error
    ).lower()


def _host_path_volumes(volumes):
    volume_mounts = []
    pod_volumes = []
    for index, (host_path, mount) in enumerate((volumes or {}).items()):
        volume_name = f"host-volume-{index}"
        volume_mounts.append({
            "name": volume_name,
            "mountPath": mount["bind"],
            "readOnly": mount.get("mode") == "ro",
        })
        pod_volumes.append({
            "name": volume_name,
            "hostPath": {"path": host_path, "type": "DirectoryOrCreate"},
        })
    return volume_mounts, pod_volumes


class KubernetesDriver(Driver):
    def __init__(self, namespace="coinjoin", reuse_namespace=False, pull_secret_path=None, in_cluster=False,
                 run_id=None):

        if in_cluster:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
        else:
            config.load_kube_config()

        self.client = client.CoreV1Api()
        # stream() swaps ``ApiClient.request`` for a websocket call while it
        # opens the exec connection. On a shared ApiClient that swap also
        # catches every plain API call another thread makes at that moment
        # (``read_namespaced_pod_status`` in a parallel download, say), so
        # exec gets its own client and the swap cannot reach anything else.
        self._exec_client = client.CoreV1Api(client.ApiClient())
        self._exec_lock = RLock()
        self._namespace = namespace
        self.reuse_namespace = reuse_namespace
        self.pull_secret_path = pull_secret_path
        self.in_cluster = in_cluster
        self.run_id = run_id

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

    def build(self, name, path, build_args=None):
        pass

    def pull(self, name):
        pass

    def resource_labels(self, name):
        """Label every resource so cleanup can find exactly this emulator's own."""
        labels = {"app": name, MANAGED_BY_LABEL: MANAGED_BY_VALUE}
        if self.run_id:
            labels[RUN_ID_LABEL] = self.run_id
        return labels

    def owner_reference(self):
        if not self.in_cluster:
            return None
        name = os.environ.get("COINJOIN_OWNER_POD_NAME")
        uid = os.environ.get("COINJOIN_OWNER_POD_UID")
        namespace = os.environ.get("COINJOIN_OWNER_POD_NAMESPACE")
        if not any((name, uid, namespace)):
            return None
        if not all((name, uid, namespace)) or namespace != self._namespace:
            raise ValueError(
                "controller pod ownership requires a complete identity in the emulator namespace"
            )
        return {"apiVersion": "v1", "kind": "Pod", "name": name, "uid": uid}

    def resource_metadata(self, name):
        metadata = {"name": name, "labels": self.resource_labels(name)}
        owner = self.owner_reference()
        if owner:
            metadata["ownerReferences"] = [owner]
        return metadata

    def cleanup_selector(self):
        """Select this run's own resources, or every managed one when no run ID is set."""
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
        if self.run_id:
            selector += f",{RUN_ID_LABEL}={self.run_id}"
        return selector

    def build_pod_manifest(self, name, image, env, ports, cpu, memory,
                            user_id=None, volumes=None, command=None, group_id=None):
        if ports is None:
            ports = {}
        if env is None:
            env = {}

        volume_mounts, pod_volumes = _host_path_volumes(volumes)

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
                            "runAsGroup": user_id if group_id is None else group_id,
                        }

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": self.resource_metadata(name),
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "image": image,
                        "imagePullPolicy": os.environ.get("KUBERNETES_IMAGE_PULL_POLICY", "Always"),
                        "name": name,
                        "ports": [
                            {"containerPort": container_port}
                            for container_port in ports.keys()
                        ],
                        "env": [
                            {"name": k, "value": v}
                            for k, v in env.items()
                        ],
                        "volumeMounts": volume_mounts,
                        "securityContext": security_context,
                        "resources": {
                            "limits": {"cpu": cpu*1.5, "memory": f"{memory*1.5}Mi"},
                            "requests": {"cpu": cpu, "memory": f"{memory}Mi"},
                        },
                        # Keep the image ENTRYPOINT unless the caller overrides it
                        **({"command": command} if command is not None else {}),
                    }
                ],
                "volumes": pod_volumes,
                "securityContext": {
                    "sysctls": [{"name": RESERVED_PORTS_SYSCTL, "value": RESERVED_PORT_RANGE}]
                },
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
        pod_manifest = self.build_pod_manifest(
            name, image, env, ports, cpu, memory, run_as_user,
            kwargs.get("volumes"), kwargs.get("command"), kwargs.get("run_as_group"),
        )
        self._create_pod(name, pod_manifest)

        try:
            pod_ip = self._wait_for_pod_ip(name)
        except StartupError as error:
            if "SysctlForbidden" not in str(error) or not _strip_reserved_ports_sysctl(pod_manifest):
                raise
            print(f"[WARNING] kubelet forbade {RESERVED_PORTS_SYSCTL} for pod {name}; recreating it without it")
            self.stop(name)
            self._create_pod(name, pod_manifest)
            pod_ip = self._wait_for_pod_ip(name)
        except Exception as e:
            print(f"Failed to get pod IP: {e}")
            raise

        service_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": self.resource_metadata(name),
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

    def _create_pod(self, name, pod_manifest):
        try:
            self.client.create_namespaced_pod(body=pod_manifest, namespace=self.namespace)
            return
        except ApiException as error:
            details = str(getattr(error, "body", "") or error)
            if error.status == 403 and "exceeded quota" in details.lower():
                raise KubernetesResourceQuotaError(
                    f"Kubernetes quota rejected pod {name} in namespace {self.namespace}: {details}"
                ) from error
            if not _is_sysctl_rejection(error) or not _strip_reserved_ports_sysctl(pod_manifest):
                raise
            print(f"[WARNING] API server rejected {RESERVED_PORTS_SYSCTL} for pod {name}; starting it without it")
        self.client.create_namespaced_pod(body=pod_manifest, namespace=self.namespace)

    def _wait_for_pod_ip(self, name):
        """Wait for a scheduled pod's IP, giving up on a terminal pod or a deadline."""
        deadline = time.monotonic() + POD_IP_WAIT_TIMEOUT_SECONDS
        while True:
            status = self.client.read_namespaced_pod_status(name=name, namespace=self.namespace).status
            if status.pod_ip:
                return status.pod_ip
            if status.phase in {"Failed", "Succeeded"}:
                detail = " ".join(
                    str(value) for value in (status.reason, status.message) if value
                )
                raise StartupError(
                    f"Pod {name} entered terminal phase {status.phase} before receiving an IP"
                    + (f": {detail}" if detail else "")
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Pod {name} did not receive an IP within {POD_IP_WAIT_TIMEOUT_SECONDS}s "
                    f"(last phase: {status.phase})"
                )
            sleep(1)

    def stop(self, name):
        """Delete the pod and service and return once the name can be reused.

        A DELETE only starts the pod's termination; creating the same name
        again before it has gone answers 409 AlreadyExists.
        """
        try:
            self.client.delete_namespaced_pod(name=name, namespace=self.namespace)
            self.client.delete_namespaced_service(
                name, namespace=self.namespace
            )
        except Exception:
            pass
        self._wait_until_gone(name)

    def _wait_until_gone(self, name):
        deadline = time.monotonic() + STOP_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self._exists(self.client.read_namespaced_pod, name) and not self._exists(
                self.client.read_namespaced_service, name
            ):
                return
            sleep(1)
        print(f"[WARNING] {name} is still terminating after {STOP_WAIT_TIMEOUT_SECONDS}s")

    def _exists(self, read, name):
        try:
            read(name=name, namespace=self.namespace)
        except ApiException as error:
            if error.status == 404:
                return False
            raise
        return True

    def download(self, name, src_path, dst_path):
        self._require_exec_ready(name)
        if src_path[-1] == "/":
            src_path = src_path[:-1]
        src_parent, src_target = os.path.split(src_path)
        # Exec reads stdout as UTF-8 text; encode the binary archive before transfer.
        exec_command = [
            "sh", "-c",
            f"tar cf - -C {shlex.quote(src_parent)} {shlex.quote(src_target)} | base64 | tr -d '\\n'",
        ]
        resp = self._exec_stream(name, exec_command, f"download {src_path}")
        encoded_chunks = []
        stderr_chunks = []
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
        try:
            while resp.is_open():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out downloading {name}:{src_path}")
                resp.update(timeout=1)
                if resp.peek_stdout():
                    encoded_chunks.append(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_chunks.append(resp.read_stderr())
        finally:
            resp.close()

        _check_tar_stderr(name, src_path, "".join(stderr_chunks))
        encoded = "".join(encoded_chunks)
        if not encoded.strip():
            raise RuntimeError(f"download of {name}:{src_path} produced an empty archive")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise RuntimeError(f"download of {name}:{src_path} returned invalid base64") from error
        with tarfile.open(fileobj=BytesIO(payload)) as tar:
            tar.extractall(dst_path)

    def pause(self, name):
        # Kubernetes has no container freezer; stop every process but the
        # container's init and the signalling shell itself.
        self._exec_checked(name, f"pause {name}", self._signal_deadline(), ["sh", "-c", "kill -STOP -1"])

    def unpause(self, name):
        self._exec_checked(name, f"unpause {name}", self._signal_deadline(), ["sh", "-c", "kill -CONT -1"])

    @staticmethod
    def _signal_deadline():
        return time.monotonic() + UPLOAD_TIMEOUT_SECONDS

    def peek(self, name, path):
        self._require_exec_ready(name)
        resp = self._exec_stream(name, ["cat", path], f"read {path}")
        output = ""
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                output += resp.read_stdout()
        resp.close()
        return output

    def container_state(self, name):
        try:
            pod = self.client.read_namespaced_pod_status(name=name, namespace=self.namespace)
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        phase = getattr(getattr(pod, "status", None), "phase", None)
        if phase is None:
            return None
        return f"pod phase {phase}"

    def _require_exec_ready(self, name):
        pod = self.client.read_namespaced_pod_status(name=name, namespace=self.namespace)
        if not pod.spec.node_name:
            raise RuntimeError(f"pod {name} is not scheduled onto a node")
        if pod.status.phase != "Running":
            raise RuntimeError(f"pod {name} is in phase {pod.status.phase}; exec requires Running")

    def _exec_stream(self, name, exec_command, action):
        # stream() swaps ApiClient.request only until the connection is opened;
        # the lock serialises that swap on the exec-only client.
        with self._exec_lock:
            try:
                return stream(
                    self._exec_client.connect_get_namespaced_pod_exec,
                    name,
                    self.namespace,
                    command=exec_command,
                    stderr=True,
                    stdin=True,
                    stdout=True,
                    tty=False,
                    _preload_content=False,
                )
            except ApiException as error:
                raise RuntimeError(f"could not {action} on pod {name}: {error}") from error

    def logs(self, name):
        return str(self.client.read_namespaced_pod_log(name=name, namespace=self.namespace))

    def get_pod_resource_usage(self, name):
        """
        Get memory usage of a pod by reading /proc/self/status.
        Returns dict with memory_mb and memory_limit_mb, or None if failed.
        """
        try:
            # Read process memory info from /proc
            resp = self._exec_stream(name, ["cat", "/proc/self/status"], "read /proc/self/status")

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
        except Exception:
            # Silently fail - pod might be terminating
            return None

    def upload(self, name, src_path, dst_path):
        deadline = time.monotonic() + UPLOAD_TIMEOUT_SECONDS
        self._wait_for_running_container(name, deadline)
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:tar") as tar:
            tar.add(src_path, arcname=dst_path)
        # write_stdin() truncated the archive whenever it exceeded a websocket
        # frame, and the loop exited before the remote tar had finished, so the
        # payload is staged in text chunks and unpacked with a checked command.
        payload = base64.b64encode(buf.getvalue()).decode("ascii")
        remote_payload = f"/tmp/coinjoin-emulator-upload-{uuid.uuid4().hex}.b64"
        try:
            for offset in range(0, len(payload), UPLOAD_COMMAND_CHUNK_SIZE):
                chunk = payload[offset:offset + UPLOAD_COMMAND_CHUNK_SIZE]
                redirect = ">" if offset == 0 else ">>"
                self._append_upload_chunk(name, dst_path, deadline, chunk, remote_payload, redirect)
            self._extract_staged_upload(name, dst_path, deadline, remote_payload)
        except Exception:
            try:
                self._remove_staged_upload(name, dst_path, deadline, remote_payload)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            raise

    def _wait_for_running_container(self, name, deadline):
        while True:
            pod = self.client.read_namespaced_pod_status(name=name, namespace=self.namespace)
            phase = pod.status.phase
            if phase in {"Failed", "Succeeded"}:
                raise StartupError(f"Cannot upload to pod {name}: terminal phase {phase}")
            for container in pod.status.container_statuses or []:
                if container.name != name:
                    continue
                if container.state.terminated is not None:
                    raise StartupError(f"Cannot upload to pod {name}: container terminated")
                if phase == "Running" and container.state.running is not None:
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting to upload to pod {name}: container is not running (phase {phase})")
            sleep(1)

    def _append_upload_chunk(self, name, dst_path, deadline, chunk, remote_payload, redirect):
        self._exec_checked(
            name,
            f"upload to {name}:{dst_path}",
            deadline,
            ["sh", "-c", f'printf "%s" "$1" {redirect} "$2"', "sh", chunk, remote_payload],
        )

    def _extract_staged_upload(self, name, dst_path, deadline, remote_payload):
        self._exec_checked(
            name,
            f"upload to {name}:{dst_path}",
            deadline,
            [
                "sh",
                "-c",
                'base64 -d "$1" | tar xf - -C /; status=$?; rm -f -- "$1"; exit "$status"',
                "sh",
                remote_payload,
            ],
        )

    def _remove_staged_upload(self, name, dst_path, deadline, remote_payload):
        self._exec_checked(name, f"upload to {name}:{dst_path}", deadline, ["rm", "-f", "--", remote_payload])

    def _exec_checked(self, name, action, deadline, exec_command):
        """Run one command and fail if it wrote to stderr or exited non-zero."""
        stderr_chunks = []
        resp = self._exec_stream(name, exec_command, action)
        try:
            while resp.is_open():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out: {action}")
                resp.update(timeout=1)
                if resp.peek_stdout():
                    resp.read_stdout()
                if resp.peek_stderr():
                    stderr_chunks.append(resp.read_stderr())
            returncode = getattr(resp, "returncode", None)
        finally:
            resp.close()
        stderr = "".join(stderr_chunks).strip()
        if returncode not in (None, 0) or stderr:
            raise RuntimeError(f"{action} failed" + (f": {stderr}" if stderr else ""))


    def cleanup(self, image_prefix=""):
        # without this, the cleaunup fails because of open websocket channel from log gathering
        # but when the fresh client is created, the log gathering fails...
        # "Working" config is letting the cleanup fail and restarting it after run...
        # fresh_client = client.CoreV1Api()
        # self.client = fresh_client
        # return

        managed = self.cleanup_selector()
        try:
            pods = self.client.list_namespaced_pod(namespace=self._namespace, label_selector=managed)
        except ApiException as e:
            print("Error listing pods:", e)
            traceback.print_exc()
            print("Cleanup failed")
            return

        for pod in pods.items:
            try:
                print(f"Deleting pod {pod.metadata.name}")
                self.client.delete_namespaced_pod(name=pod.metadata.name, namespace=self._namespace)
                print(f"Deleted pod {pod.metadata.name}")
            except ApiException as e:
                print(f"Failed to delete pod {pod.metadata.name}: {e}")
        services = self.client.list_namespaced_service(namespace=self._namespace, label_selector=managed)
        for service in services.items:
            try:
                print("Deleting service", service.metadata.name)
                self.client.delete_namespaced_service(name=service.metadata.name, namespace=self._namespace)
                print("Deleted service", service.metadata.name)
            except ApiException as e:
                print(f"Failed to delete service {service.metadata.name}: {e}")

        if not self.reuse_namespace:
            try:
                print(f"Deleting namespace {self._namespace}")
                self.client.delete_namespace(
                    name=self._namespace, body=client.V1DeleteOptions()
                )
            except ApiException:
                pass
