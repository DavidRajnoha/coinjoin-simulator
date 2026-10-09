"""Kubernetes driver contracts that keep a shared namespace usable."""

import base64
import io
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from manager.driver import RESERVED_PORT_RANGE, RESERVED_PORTS_SYSCTL
from manager.driver.kubernetes import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    UPLOAD_COMMAND_CHUNK_SIZE,
    KubernetesDriver,
)
from manager.exceptions import KubernetesResourceQuotaError, StartupError


def driver(**overrides: object) -> KubernetesDriver:
    instance = object.__new__(KubernetesDriver)
    instance.client = Mock()
    instance.client.read_namespaced_pod_status.return_value = running_pod()
    instance._exec_client = Mock()  # pylint: disable=protected-access
    instance._exec_lock = RLock()  # pylint: disable=protected-access
    instance._namespace = "coinjoin"  # pylint: disable=protected-access
    instance.reuse_namespace = True
    instance.pull_secret_path = None
    instance.in_cluster = False
    instance.run_id = None
    for key, value in overrides.items():
        setattr(instance, key, value)
    return instance


def test_pods_and_services_are_labelled_with_the_run() -> None:
    manifest = driver(run_id="run-42").build_pod_manifest(
        "btc-node", "btc-node:latest", {}, {18443: 18443}, 1.0, 512
    )

    assert manifest["metadata"]["labels"] == {
        "app": "btc-node",
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        "coinjoin.run-id": "run-42",
    }
    assert "ownerReferences" not in manifest["metadata"]


def test_in_cluster_controller_pod_owns_created_pods_and_services(monkeypatch) -> None:
    monkeypatch.setenv("COINJOIN_OWNER_POD_NAME", "coinjoin-s3-run-42-abcde")
    monkeypatch.setenv("COINJOIN_OWNER_POD_UID", "controller-uid")
    monkeypatch.setenv("COINJOIN_OWNER_POD_NAMESPACE", "coinjoin")
    instance = driver(in_cluster=True, run_id="run-42")
    instance._wait_for_pod_ip = Mock(return_value="10.0.0.7")  # pylint: disable=protected-access

    instance.run("btc-node", "btc-node:latest", ports={18443: 18443}, cpu=1.0, memory=512)

    expected_owner = {
        "apiVersion": "v1",
        "kind": "Pod",
        "name": "coinjoin-s3-run-42-abcde",
        "uid": "controller-uid",
    }
    pod = instance.client.create_namespaced_pod.call_args.kwargs["body"]
    service = instance.client.create_namespaced_service.call_args.kwargs["body"]
    assert pod["metadata"]["ownerReferences"] == [expected_owner]
    assert service["metadata"]["ownerReferences"] == [expected_owner]
    assert pod["metadata"]["labels"]["coinjoin.run-id"] == "run-42"
    assert service["metadata"]["labels"]["coinjoin.run-id"] == "run-42"


@pytest.mark.parametrize("owner_namespace", ["", "other-namespace"])
def test_controller_pod_owner_must_be_in_the_emulator_namespace(monkeypatch, owner_namespace) -> None:
    monkeypatch.setenv("COINJOIN_OWNER_POD_NAME", "controller-pod")
    monkeypatch.setenv("COINJOIN_OWNER_POD_UID", "controller-uid")
    monkeypatch.setenv("COINJOIN_OWNER_POD_NAMESPACE", owner_namespace)
    instance = driver(in_cluster=True)

    with pytest.raises(ValueError, match="complete identity in the emulator namespace"):
        instance.run("btc-node", "btc-node:latest", ports={}, cpu=1.0, memory=512)

    instance.client.create_namespaced_pod.assert_not_called()


def test_a_host_path_is_mounted_and_a_command_overrides_the_entrypoint() -> None:
    manifest = driver().build_pod_manifest(
        "btc-node",
        "btc-node:latest",
        {},
        {18443: 18443},
        1.0,
        512,
        volumes={"/data/btc": {"bind": "/home/bitcoin/data", "mode": "rw"}},
        command=["./run.sh", "-prune=0"],
    )

    container = manifest["spec"]["containers"][0]
    assert manifest["spec"]["volumes"] == [
        {"name": "host-volume-0", "hostPath": {"path": "/data/btc", "type": "DirectoryOrCreate"}}
    ]
    assert container["volumeMounts"] == [
        {"name": "host-volume-0", "mountPath": "/home/bitcoin/data", "readOnly": False}
    ]
    assert container["command"] == ["./run.sh", "-prune=0"]


def test_cleanup_only_touches_resources_this_emulator_created() -> None:
    instance = driver()
    instance.client.list_namespaced_pod.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="btc-node"))]
    )
    instance.client.list_namespaced_service.return_value = SimpleNamespace(items=[])

    instance.cleanup()

    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
    assert instance.client.list_namespaced_pod.call_args.kwargs["label_selector"] == selector
    assert instance.client.list_namespaced_service.call_args.kwargs["label_selector"] == selector
    instance.client.delete_namespaced_pod.assert_called_once_with(
        name="btc-node", namespace="coinjoin"
    )


def test_cleanup_leaves_a_concurrent_run_in_the_same_namespace_alone() -> None:
    instance = driver(run_id="run-42")
    instance.client.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    instance.client.list_namespaced_service.return_value = SimpleNamespace(items=[])

    instance.cleanup()

    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},coinjoin.run-id=run-42"
    assert instance.client.list_namespaced_pod.call_args.kwargs["label_selector"] == selector
    assert instance.client.list_namespaced_service.call_args.kwargs["label_selector"] == selector


def test_a_quota_rejection_is_reported_as_a_quota_error() -> None:
    instance = driver()
    rejection = ApiException(status=403)
    rejection.body = 'pods "btc-node" is forbidden: exceeded quota: cpu'
    instance.client.create_namespaced_pod.side_effect = rejection

    with pytest.raises(KubernetesResourceQuotaError):
        instance.run("btc-node", "btc-node:latest", ports={18443: 18443}, cpu=1.0, memory=512)


def test_a_pod_that_terminates_before_it_gets_an_ip_fails_the_run() -> None:
    instance = driver()
    instance.client.read_namespaced_pod_status.return_value = SimpleNamespace(
        status=SimpleNamespace(pod_ip=None, phase="Failed", reason="Evicted", message="no memory")
    )

    with pytest.raises(StartupError, match="terminal phase Failed"):
        instance._wait_for_pod_ip("btc-node")  # pylint: disable=protected-access


def test_waiting_for_a_pod_ip_has_a_deadline() -> None:
    instance = driver()
    instance.client.read_namespaced_pod_status.return_value = SimpleNamespace(
        status=SimpleNamespace(pod_ip=None, phase="Pending", reason=None, message=None)
    )

    with patch("manager.driver.kubernetes.time.monotonic", side_effect=[0.0, 10_000.0]):
        with pytest.raises(TimeoutError, match="did not receive an IP"):
            instance._wait_for_pod_ip("btc-node")  # pylint: disable=protected-access


def running_pod() -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(node_name="node-1"),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(
                name="jcs-000", state=SimpleNamespace(running=SimpleNamespace(), terminated=None)
            )],
        ),
    )


class FakeStream:
    """Exec output delivered in text chunks, followed by a closed connection."""

    def __init__(self, stdout: str = "", stderr: str = "", chunks=()) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.chunks = iter(chunks)
        self.open = True

    def is_open(self) -> bool:
        return self.open

    def update(self, timeout: int) -> None:
        if not self.stdout and not self.stderr:
            self.stdout = next(self.chunks, "")
            self.open = bool(self.stdout)

    def peek_stdout(self) -> bool:
        return bool(self.stdout)

    def read_stdout(self) -> str:
        stdout, self.stdout = self.stdout, ""
        return stdout

    def peek_stderr(self) -> bool:
        return bool(self.stderr)

    def read_stderr(self) -> str:
        stderr, self.stderr = self.stderr, ""
        return stderr

    def close(self) -> None:
        self.open = False


def test_download_preserves_binary_files_across_text_chunks(tmp_path) -> None:
    instance = driver()
    source = tmp_path / "directory with 'quotes'" / "logs"
    source.mkdir(parents=True)
    contents = bytes(range(256))
    (source / "binary.dat").write_bytes(contents)

    def run_command(api, name, namespace, command, **kwargs):
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=2)
        return FakeStream(chunks=[result.stdout[i:i + 73] for i in range(0, len(result.stdout), 73)])

    with patch("manager.driver.kubernetes.stream", side_effect=run_command):
        instance.download("jcs-000", str(source) + "/", str(tmp_path / "dst"))

    assert (tmp_path / "dst/logs/binary.dat").read_bytes() == contents


def test_pause_and_unpause_signal_every_process_in_the_pod() -> None:
    commands = []

    def run_command(api, name, namespace, command, **kwargs):
        commands.append(command)
        return FakeStream()

    with patch("manager.driver.kubernetes.stream", side_effect=run_command):
        driver().pause("btc-node")
        driver().unpause("btc-node")

    assert commands == [["sh", "-c", "kill -STOP -1"], ["sh", "-c", "kill -CONT -1"]]


def test_a_pause_that_fails_inside_the_pod_is_reported() -> None:
    response = FakeStream(stderr="sh: kill: Operation not permitted\n")
    with patch("manager.driver.kubernetes.stream", return_value=response):
        with pytest.raises(RuntimeError, match="pause btc-node failed: sh: kill: Operation not permitted"):
            driver().pause("btc-node")


def test_download_rejects_invalid_base64(tmp_path) -> None:
    with patch("manager.driver.kubernetes.stream", return_value=FakeStream(stdout="broken!")):
        with pytest.raises(RuntimeError, match="invalid base64"):
            driver().download("jcs-000", "/logs/", str(tmp_path))


def test_download_rejects_invalid_base64_after_valid_chunks(tmp_path) -> None:
    encoded = archive()
    response = FakeStream(chunks=[encoded[:10], encoded[10:], "!!!!"])
    with patch("manager.driver.kubernetes.stream", return_value=response):
        with pytest.raises(RuntimeError, match="invalid base64"):
            driver().download("jcs-000", "/logs/", str(tmp_path))

    assert not (tmp_path / "logs").exists()


def archive() -> str:
    payload = io.BytesIO()
    contents = b"hello"
    with tarfile.open(fileobj=payload, mode="w") as tar:
        entry = tarfile.TarInfo("logs/run.log")
        entry.size = len(contents)
        tar.addfile(entry, io.BytesIO(contents))
    return base64.b64encode(payload.getvalue()).decode("ascii")


@pytest.mark.parametrize("warning", [
    "tar: logs/run.log: file changed as we read it",
    "tar: logs/rpc.sock: socket ignored",
    "tar: Removing leading `/' from member names",
    "tar: Removing leading `/' from hard link targets",
])
def test_download_keeps_an_archive_with_a_benign_tar_warning(tmp_path, warning) -> None:
    response = FakeStream(stdout=archive(), stderr=warning + "\n")
    with patch("manager.driver.kubernetes.stream", return_value=response):
        driver().download("jcs-000", "/logs/", str(tmp_path))

    assert (tmp_path / "logs/run.log").read_text() == "hello"


def test_download_reports_tar_errors_even_with_a_valid_archive(tmp_path) -> None:
    diagnostics = "tar: logs/run.log: file changed as we read it\ntar: logs: Cannot open\n"
    with patch("manager.driver.kubernetes.stream", return_value=FakeStream(stdout=archive(), stderr=diagnostics)):
        with pytest.raises(RuntimeError, match="Cannot open"):
            driver().download("jcs-000", "/logs/", str(tmp_path))

    assert not (tmp_path / "logs").exists()


def test_download_rejects_empty_output(tmp_path) -> None:
    with patch("manager.driver.kubernetes.stream", return_value=FakeStream()):
        with pytest.raises(RuntimeError, match="empty archive"):
            driver().download("jcs-000", "/logs/", str(tmp_path))


def test_download_timeout_closes_the_connection(tmp_path) -> None:
    response = FakeStream()
    with patch("manager.driver.kubernetes.stream", return_value=response):
        with patch("manager.driver.kubernetes.time.monotonic", side_effect=[0.0, 10_000.0]):
            with pytest.raises(TimeoutError, match="Timed out downloading"):
                driver().download("jcs-000", "/logs/", str(tmp_path))

    assert not response.is_open()


def test_download_closes_the_connection_on_a_read_error(tmp_path) -> None:
    response = FakeStream()
    with patch.object(response, "update", side_effect=OSError("connection lost")):
        with patch("manager.driver.kubernetes.stream", return_value=response):
            with pytest.raises(OSError, match="connection lost"):
                driver().download("jcs-000", "/logs/", str(tmp_path))

    assert not response.is_open()


def test_exec_opens_connections_serially_but_keeps_streams_independent() -> None:
    instance = driver()
    opening_first = Event()
    attempting_second = Event()
    opening_second = Event()
    release_first = Event()

    def open_stream(api, name, namespace, **kwargs):
        if name == "first":
            opening_first.set()
            assert release_first.wait(2)
        else:
            opening_second.set()
        return FakeStream()

    def open_second():
        attempting_second.set()
        return instance._exec_stream("second", ["cat", "/logs"], "read logs")

    with patch("manager.driver.kubernetes.stream", side_effect=open_stream):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(instance._exec_stream, "first", ["cat", "/logs"], "read logs")
            try:
                assert opening_first.wait(2)
                second = pool.submit(open_second)
                assert attempting_second.wait(2)
                assert not opening_second.wait(0.05)
            finally:
                release_first.set()
            first_stream = first.result(timeout=2)
            second_stream = second.result(timeout=2)

    assert opening_second.is_set()
    assert first_stream is not second_stream
    assert first_stream.is_open() and second_stream.is_open()


def test_exec_streams_open_on_a_client_no_plain_api_call_shares() -> None:
    """stream() swaps ApiClient.request while it connects; a plain call on the same
    client in another thread (the pod-status check of a parallel download) would be
    routed into the websocket path and fail, so exec must use its own client."""
    instance = driver()
    opened_with = []

    def open_stream(api_method, name, namespace, **kwargs):
        opened_with.append(api_method)
        return FakeStream()

    with patch("manager.driver.kubernetes.stream", side_effect=open_stream):
        instance._exec_stream("jcs-000", ["cat", "/logs"], "read logs")

    assert opened_with == [instance._exec_client.connect_get_namespaced_pod_exec]
    instance.client.connect_get_namespaced_pod_exec.assert_not_called()


def test_a_fresh_driver_keeps_exec_and_plain_api_clients_apart() -> None:
    with patch("manager.driver.kubernetes.config.load_kube_config"):
        instance = KubernetesDriver(namespace="coinjoin")

    assert instance._exec_client is not instance.client
    assert instance._exec_client.api_client is not instance.client.api_client


def test_exec_api_failure_includes_pod_and_action() -> None:
    instance = driver()
    with patch("manager.driver.kubernetes.stream", side_effect=ApiException(status=404)):
        with pytest.raises(RuntimeError, match="could not read logs on pod missing"):
            instance._exec_stream("missing", ["cat", "/logs"], "read logs")

    with patch("manager.driver.kubernetes.stream", return_value=FakeStream()):
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(instance._exec_stream, "next", ["cat", "/logs"], "read logs").result(timeout=2)
    assert response.is_open()


@pytest.mark.parametrize("method", ["download", "peek"])
@pytest.mark.parametrize("node,phase,message", [
    (None, "Pending", "not scheduled"),
    ("node-1", "Pending", "phase Pending"),
    ("node-1", "Failed", "phase Failed"),
    ("node-1", "Succeeded", "phase Succeeded"),
])
def test_reading_an_unavailable_pod_is_rejected_before_exec(tmp_path, method, node, phase, message) -> None:
    instance = driver()
    instance.client.read_namespaced_pod_status.return_value = SimpleNamespace(
        spec=SimpleNamespace(node_name=node), status=SimpleNamespace(phase=phase)
    )
    arguments = ("jcs-000", "/logs/", str(tmp_path)) if method == "download" else ("jcs-000", "/logs/run.log")
    with patch("manager.driver.kubernetes.stream") as open_stream:
        with pytest.raises(RuntimeError, match=message):
            getattr(instance, method)(*arguments)

    open_stream.assert_not_called()


def test_upload_stages_the_archive_in_chunks_and_unpacks_it(tmp_path) -> None:
    instance = driver()
    source = tmp_path / "scenario.json"
    contents = b'{"large-config":"' + (b"x" * UPLOAD_COMMAND_CHUNK_SIZE) + b'"}'
    source.write_bytes(contents)
    commands = []

    def record(api, name, namespace, command, **kwargs):
        commands.append(command)
        return FakeStream()

    with patch("manager.driver.kubernetes.stream", side_effect=record):
        instance.upload("jcs-000", str(source), "/app/scenario.json")

    append_commands = commands[:-1]
    assert len(append_commands) >= 2
    assert append_commands[0][2] == 'printf "%s" "$1" > "$2"'
    assert all(command[2] == 'printf "%s" "$1" >> "$2"' for command in append_commands[1:])
    assert "base64 -d" in commands[-1][2] and "tar xf -" in commands[-1][2]

    payload = base64.b64decode("".join(command[4] for command in append_commands))
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        member = archive.getmember("app/scenario.json")
        extracted = archive.extractfile(member)
        assert extracted is not None
        assert extracted.read() == contents


def test_upload_waits_for_the_container_even_when_the_pod_phase_is_running(tmp_path) -> None:
    instance = driver()
    waiting = running_pod()
    waiting.status.container_statuses[0].state.running = None
    instance.client.read_namespaced_pod_status.side_effect = [waiting, running_pod()]
    source = tmp_path / "scenario.json"
    source.write_text("{}", encoding="utf-8")

    with (
        patch("manager.driver.kubernetes.stream", return_value=FakeStream()) as open_stream,
        patch("manager.driver.kubernetes.sleep") as sleep,
    ):
        sleep.side_effect = lambda _seconds: open_stream.assert_not_called()
        instance.upload("jcs-000", str(source), "/app/scenario.json")

    sleep.assert_called_once_with(1)
    assert open_stream.called


@pytest.mark.parametrize("phase", ["Failed", "Succeeded"])
def test_upload_rejects_a_terminal_pod_without_exec(tmp_path, phase) -> None:
    instance = driver()
    instance.client.read_namespaced_pod_status.return_value.status.phase = phase
    with patch("manager.driver.kubernetes.stream") as open_stream:
        with pytest.raises(StartupError, match=f"terminal phase {phase}"):
            instance.upload("jcs-000", str(tmp_path / "unused"), "/app/scenario.json")
    open_stream.assert_not_called()


def test_upload_rejects_a_terminated_container_without_exec(tmp_path) -> None:
    instance = driver()
    state = instance.client.read_namespaced_pod_status.return_value.status.container_statuses[0].state
    state.running = None
    state.terminated = SimpleNamespace(exit_code=1)
    with patch("manager.driver.kubernetes.stream") as open_stream:
        with pytest.raises(StartupError, match="container terminated"):
            instance.upload("jcs-000", str(tmp_path / "unused"), "/app/scenario.json")
    open_stream.assert_not_called()


def test_upload_waiting_for_a_container_has_a_deadline(tmp_path) -> None:
    instance = driver()
    instance.client.read_namespaced_pod_status.return_value.status = SimpleNamespace(
        phase="Pending", container_statuses=None
    )
    with (
        patch("manager.driver.kubernetes.time.monotonic", side_effect=[0.0, 1000.0]),
        patch("manager.driver.kubernetes.stream") as open_stream,
        pytest.raises(TimeoutError, match="Timed out waiting to upload.*Pending"),
    ):
        instance.upload("jcs-000", str(tmp_path / "unused"), "/app/scenario.json")
    open_stream.assert_not_called()


def test_upload_fails_when_the_remote_command_writes_to_stderr(tmp_path) -> None:
    instance = driver()
    source = tmp_path / "scenario.json"
    source.write_text("{}", encoding="utf-8")

    with patch(
        "manager.driver.kubernetes.stream",
        return_value=FakeStream(stderr="tar: /app: Cannot open\n"),
    ):
        with pytest.raises(RuntimeError, match="upload to jcs-000:/app/scenario.json failed"):
            instance.upload("jcs-000", str(source), "/app/scenario.json")


def test_the_image_pull_policy_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("KUBERNETES_IMAGE_PULL_POLICY", "IfNotPresent")

    manifest = driver().build_pod_manifest("btc-node", "btc-node:latest", {}, {}, 1.0, 512)

    assert manifest["spec"]["containers"][0]["imagePullPolicy"] == "IfNotPresent"


def test_the_image_pull_policy_defaults_to_always() -> None:
    manifest = driver().build_pod_manifest("btc-node", "btc-node:latest", {}, {}, 1.0, 512)

    assert manifest["spec"]["containers"][0]["imagePullPolicy"] == "Always"


def test_the_wasabi_service_ports_are_reserved_for_the_pod() -> None:
    manifest = driver().build_pod_manifest("wasabi-backend", "backend:latest", {}, {}, 1.0, 512)

    assert manifest["spec"]["securityContext"]["sysctls"] == [
        {"name": RESERVED_PORTS_SYSCTL, "value": RESERVED_PORT_RANGE}
    ]


def test_an_api_server_that_forbids_the_sysctl_gets_a_pod_without_it() -> None:
    instance = driver(in_cluster=True)
    rejection = ApiException(status=403)
    rejection.body = "forbidden sysctl: net.ipv4.ip_local_reserved_ports"
    created_manifests = []

    def reject_then_record(*, body, namespace):
        assert namespace == "coinjoin"
        created_manifests.append(deepcopy(body))
        if len(created_manifests) == 1:
            raise rejection

    instance.client.create_namespaced_pod.side_effect = reject_then_record
    instance._wait_for_pod_ip = Mock(return_value="10.0.0.7")  # pylint: disable=protected-access

    endpoint = instance.run("wasabi-backend", "backend:latest", {}, {}, 1.0, 512)

    assert endpoint == ("wasabi-backend.coinjoin.svc.cluster.local", {}, None)
    assert len(created_manifests) == 2
    assert "securityContext" in created_manifests[0]["spec"]
    assert "securityContext" not in created_manifests[1]["spec"]


def test_kubelet_sysctl_rejection_recreates_the_pod_without_it() -> None:
    instance = driver(in_cluster=True)
    created_manifests = []

    def record_manifest(*, body, namespace):
        assert namespace == "coinjoin"
        created_manifests.append(deepcopy(body))

    instance.client.create_namespaced_pod.side_effect = record_manifest
    instance.client.read_namespaced_pod.side_effect = ApiException(status=404)
    instance.client.read_namespaced_service.side_effect = ApiException(status=404)
    instance._wait_for_pod_ip = Mock(  # pylint: disable=protected-access
        side_effect=[StartupError("Pod wasabi-backend: SysctlForbidden"), "10.0.0.7"]
    )

    endpoint = instance.run("wasabi-backend", "backend:latest", {}, {}, 1.0, 512)

    assert endpoint == ("wasabi-backend.coinjoin.svc.cluster.local", {}, None)
    assert len(created_manifests) == 2
    assert "securityContext" in created_manifests[0]["spec"]
    assert "securityContext" not in created_manifests[1]["spec"]


def test_stop_returns_only_after_the_pod_and_service_are_gone() -> None:
    instance = driver()
    gone = ApiException(status=404)
    instance.client.read_namespaced_pod.side_effect = [running_pod(), running_pod(), gone]
    instance.client.read_namespaced_service.side_effect = [gone]

    with patch("manager.driver.kubernetes.sleep") as sleep:
        instance.stop("wasabi-coordinator")

    instance.client.delete_namespaced_pod.assert_called_once_with(name="wasabi-coordinator", namespace="coinjoin")
    instance.client.delete_namespaced_service.assert_called_once_with("wasabi-coordinator", namespace="coinjoin")
    assert instance.client.read_namespaced_pod.call_count == 3
    assert sleep.call_count == 2


def test_stop_gives_up_waiting_after_the_deadline() -> None:
    instance = driver()
    instance.client.read_namespaced_pod.return_value = running_pod()

    with patch("manager.driver.kubernetes.sleep"), patch(
        "manager.driver.kubernetes.time.monotonic", side_effect=[0.0, 0.0, 1000.0]
    ):
        instance.stop("wasabi-coordinator")

    assert instance.client.read_namespaced_pod.call_count == 1
