"""Coordinator startup: a known transient failure must not lose the whole run."""

import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from kubernetes.client.exceptions import ApiException

from manager.driver.kubernetes import KubernetesDriver
from manager.engine.wasabi_engine import WasabiEngine, coordinator_retry_reason
from manager.wasabi_backend_factory import BackendArchitecture


def engine() -> WasabiEngine:
    instance = object.__new__(WasabiEngine)
    instance.args = SimpleNamespace(image_prefix="", btc_node_ip="", proxy="", in_cluster=False, control_ip="localhost")
    instance.backend_architecture = BackendArchitecture.SPLIT
    instance.node = SimpleNamespace(internal_ip="10.0.0.2")
    instance.driver = Mock()
    instance.driver.run.return_value = ("wasabi-coordinator", {37128: 37128}, None)
    instance.versions = {"2.6.0"}
    instance.scenario = SimpleNamespace(backend=None)
    return instance


def test_a_transient_startup_failure_is_recognized() -> None:
    assert coordinator_retry_reason("Bitcoin Node is not fully synchronized") is not None
    assert coordinator_retry_reason("System.Net.Sockets: address already in use") is not None
    assert coordinator_retry_reason("Unhandled configuration error") is None


def test_the_coordinator_is_restarted_after_a_transient_failure() -> None:
    instance = engine()
    instance.driver.logs.return_value = "Bitcoin Node is not fully synchronized"
    coordinator = Mock()
    coordinator.wait_ready.side_effect = [TimeoutError("not ready"), None]

    with (
        patch("manager.engine.wasabi_engine.create_coordinator", return_value=coordinator),
        patch("manager.engine.wasabi_engine.sleep"),
    ):
        instance.start_wasabi_coordinator()

    instance.driver.stop.assert_called_once_with("wasabi-coordinator")
    assert instance.driver.run.call_count == 2
    assert [call.args[2] for call in instance.driver.upload.call_args_list] == [
        "/home/wasabi/coordinator-config.json",
        "/home/wasabi/coordinator-config.ready",
    ] * 2


def test_an_unknown_startup_failure_fails_the_run_with_the_logs() -> None:
    instance = engine()
    instance.driver.logs.return_value = "Unhandled configuration error"
    coordinator = Mock()
    coordinator.wait_ready.side_effect = TimeoutError("not ready")

    with (
        patch("manager.engine.wasabi_engine.create_coordinator", return_value=coordinator),
        patch("manager.engine.wasabi_engine.sleep"),
    ):
        with pytest.raises(Exception, match="Unhandled configuration error"):
            instance.start_wasabi_coordinator()

    instance.driver.stop.assert_not_called()


def test_the_coordinator_config_carries_the_scenario_backend_overrides() -> None:
    instance = engine()
    instance.scenario = SimpleNamespace(backend={"MiningFeeRate": 5})
    uploads = []

    def record_upload(name, source, destination):
        uploads.append((name, destination, Path(source).read_text(encoding="utf-8")))

    instance.driver.upload.side_effect = record_upload

    instance.upload_coordinator_config("2.6.0")

    (name, destination, contents), confirmation = uploads
    assert name == "wasabi-coordinator"
    assert destination == "/home/wasabi/coordinator-config.json"
    config = json.loads(contents)
    assert config["MiningFeeRate"] == 5
    assert config["BitcoinCoreRpcEndPoint"] == "10.0.0.2:18443"
    assert confirmation == ("wasabi-coordinator", "/home/wasabi/coordinator-config.ready", "")
    assert all(not Path(call.args[1]).exists() for call in instance.driver.upload.call_args_list)


def test_a_failed_config_upload_is_not_confirmed() -> None:
    instance = engine()
    instance.driver.upload.side_effect = RuntimeError("upload interrupted")

    with pytest.raises(RuntimeError, match="upload interrupted"):
        instance.upload_coordinator_config("2.6.0")

    instance.driver.upload.assert_called_once()
    assert instance.driver.upload.call_args.args[2] == "/home/wasabi/coordinator-config.json"
    assert not Path(instance.driver.upload.call_args.args[1]).exists()


@pytest.mark.parametrize("container_start_delay", [0, 3], ids=["running", "pending-with-ip"])
def test_the_coordinator_can_start_while_its_kubernetes_image_is_still_pulling(
    monkeypatch, tmp_path, container_start_delay: int
) -> None:
    instance = engine()
    instance.args.in_cluster = True
    driver = object.__new__(KubernetesDriver)
    driver.client = Mock()
    driver._exec_client = Mock()
    driver._exec_lock = RLock()
    driver._namespace = "coinjoin"
    driver.in_cluster = True
    driver.reuse_namespace = True
    driver.pull_secret_path = None
    driver.run_id = None
    instance.driver = driver
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    def pod_status(**_kwargs):
        return SimpleNamespace(
            spec=SimpleNamespace(node_name="worker-1"),
            status=SimpleNamespace(
                pod_ip="10.0.0.4",
                phase="Running" if elapsed >= container_start_delay else "Pending",
                container_statuses=[SimpleNamespace(
                    name="wasabi-coordinator",
                    state=SimpleNamespace(
                        running=SimpleNamespace() if elapsed >= container_start_delay else None,
                        terminated=None,
                    ),
                )],
            ),
        )

    def open_exec_stream(*_args, **_kwargs):
        if elapsed < container_start_delay:
            raise ApiException(status=500, reason='container not found ("wasabi-coordinator")')
        response = Mock()
        response.is_open.return_value = False
        response.returncode = 0
        return response

    def http_get(*_args, **_kwargs):
        if elapsed < container_start_delay:
            raise requests.ConnectionError("coordinator container has not started")
        response = Mock()
        response.json.return_value = {}
        return response

    driver.client.read_namespaced_pod_status.side_effect = pod_status
    monkeypatch.setattr("manager.driver.kubernetes.stream", open_exec_stream)
    monkeypatch.setattr("manager.driver.kubernetes.sleep", sleep)
    monkeypatch.setattr("manager.driver.kubernetes.time.monotonic", lambda: elapsed)
    monkeypatch.setattr("manager.engine.wasabi_engine.sleep", sleep)
    monkeypatch.setattr("manager.engine.wasabi_engine.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("manager.wasabi_coordinator.sleep", sleep)
    monkeypatch.setattr("manager.wasabi_coordinator.monotonic", lambda: elapsed)
    monkeypatch.setattr("manager.wasabi_coordinator.requests.get", http_get)

    instance.start_wasabi_coordinator()

    assert elapsed >= container_start_delay
    assert instance.coordinator is not None
    driver.client.create_namespaced_pod.assert_called_once()
    driver.client.delete_namespaced_pod.assert_not_called()


def test_the_split_distributor_is_pointed_at_the_coordinator() -> None:
    instance = engine()
    instance.backend = SimpleNamespace(internal_ip="10.0.0.3")
    instance.coordinator = SimpleNamespace(internal_ip="10.0.0.4")
    instance.args.wasabi_backend_ip = ""
    instance.scenario = SimpleNamespace(distributor_version=None, default_version="2.6.0")
    instance.driver.run.return_value = ("wasabi-client-distributor", {37128: 37131}, None)
    distributor = Mock()
    distributor.wait_wallet.return_value = True
    instance.init_wasabi_client = Mock(return_value=distributor)
    instance.args.distributor_startup_timeout = 60

    instance.start_distributor()

    assert instance.driver.run.call_args.kwargs["env"]["ADDR_WASABI_COORDINATOR"] == "10.0.0.4"
