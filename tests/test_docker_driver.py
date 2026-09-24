"""Docker driver contracts for endpoint resolution and artifact collection."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from manager.driver import managed_label_filters, managed_labels, warn_if_host_ports_unreserved
from manager.driver.docker import DockerDriver


def driver() -> DockerDriver:
    instance = object.__new__(DockerDriver)
    instance.client = Mock()
    instance._namespace = "coinjoin"  # pylint: disable=protected-access
    instance._run_id = "run-42"  # pylint: disable=protected-access
    instance.__dict__["network"] = SimpleNamespace(id="net-1")
    return instance


def test_build_forwards_dockerfile_build_arguments() -> None:
    instance = driver()

    instance.build(
        "joinmarket-client-server",
        "/source",
        build_args={"JOINMARKET_BASE_IMAGE": "registry/joinmarket-base:latest"},
    )

    instance.client.images.build.assert_called_once_with(
        path="/source",
        tag="joinmarket-client-server",
        rm=True,
        nocache=True,
        buildargs={"JOINMARKET_BASE_IMAGE": "registry/joinmarket-base:latest"},
    )


def test_containers_are_addressed_by_name_on_the_bridge_network() -> None:
    instance = driver()

    endpoint = instance.run("jcs-000", "jcs:latest", ports={28183: 28185}, cpu=0.1, memory=64)

    assert endpoint == ("jcs-000", {28183: 28185}, None)
    assert instance.client.containers.run.call_args.kwargs["labels"] == managed_labels(
        "coinjoin", "run-42"
    )


def test_a_failed_download_is_reported_instead_of_ignored() -> None:
    import docker

    instance = driver()
    instance.client.containers.get.side_effect = docker.errors.NotFound("no such container")

    with pytest.raises(RuntimeError, match="Failed to download jcs-000:/logs"):
        instance.download("jcs-000", "/logs", "/tmp/out")


def test_a_running_container_is_paused_while_it_is_archived(tmp_path) -> None:
    instance = driver()
    container = instance.client.containers.get.return_value
    container.status = "running"
    container.get_archive.return_value = (iter([tar_bytes()]), {})

    instance.download("jcs-000", "/logs", str(tmp_path))

    container.pause.assert_called_once_with()
    container.unpause.assert_called_once_with()


def test_pause_and_unpause_freeze_the_container() -> None:
    instance = driver()
    container = instance.client.containers.get.return_value

    instance.pause("btc-node")
    container.pause.assert_called_once_with()

    instance.unpause("btc-node")
    container.unpause.assert_called_once_with()


def test_a_failed_pause_is_reported() -> None:
    import docker

    instance = driver()
    instance.client.containers.get.return_value.pause.side_effect = docker.errors.APIError("already paused")

    with pytest.raises(RuntimeError, match="Failed to pause btc-node"):
        instance.pause("btc-node")


def tar_bytes() -> bytes:
    import io
    import tarfile

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        info = tarfile.TarInfo("logs")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    return payload.getvalue()


def test_stopped_containers_are_still_found_during_cleanup() -> None:
    instance = driver()
    instance.client.containers.list.return_value = []
    instance.client.networks.list.return_value = []

    instance.cleanup()

    assert instance.client.containers.list.call_args.kwargs == {
        "all": True,
        "filters": {"label": managed_label_filters("coinjoin", "run-42")},
    }


def test_cleanup_stops_only_containers_returned_by_the_ownership_filter() -> None:
    instance = driver()
    instance.client.containers.list.return_value = [
        SimpleNamespace(name="btc-node"),
        SimpleNamespace(name="wasabi-client-000"),
    ]
    instance.client.networks.list.return_value = []
    selected = []
    instance.stop_many = lambda names: selected.extend(names)

    instance.cleanup()

    assert selected == ["btc-node", "wasabi-client-000"]
    assert instance.client.containers.list.call_args.kwargs["filters"] == {
        "label": managed_label_filters("coinjoin", "run-42")
    }


def test_get_pod_resource_usage_reports_memory(monkeypatch) -> None:
    """The engine samples this every resource check; Docker used to raise."""
    driver = DockerDriver.__new__(DockerDriver)
    container = SimpleNamespace(
        stats=lambda stream=False: {
            "memory_stats": {"usage": 128 * 1024 * 1024, "limit": 256 * 1024 * 1024}
        }
    )
    driver.client = SimpleNamespace(
        containers=SimpleNamespace(get=lambda name: container)
    )

    stats = driver.get_pod_resource_usage("wasabi-client-000")

    assert stats["memory_mb"] == 128
    assert stats["memory_limit_mb"] == 256
    assert stats["memory_percent"] == 50


def test_get_pod_resource_usage_returns_none_without_stats() -> None:
    driver = DockerDriver.__new__(DockerDriver)
    container = SimpleNamespace(stats=lambda stream=False: {})
    driver.client = SimpleNamespace(
        containers=SimpleNamespace(get=lambda name: container)
    )

    assert driver.get_pod_resource_usage("wasabi-client-000") is None


@pytest.mark.parametrize(
    ("reserved", "warned"),
    [("", True), ("37127-37200", True), ("8080,37000-37300", False), ("37127-37260", False)],
)
def test_host_port_reservation_warning(tmp_path, capsys, reserved: str, warned: bool) -> None:
    sysctl = tmp_path / "ip_local_reserved_ports"
    sysctl.write_text(reserved + "\n", encoding="ascii")

    warn_if_host_ports_unreserved(path=str(sysctl))

    assert ("address already in use" in capsys.readouterr().out) is warned


@pytest.mark.parametrize(("daemon_host", "warned"), [("tcp://dind:2375", False), ("unix:///var/run/docker.sock", True)])
def test_host_port_reservation_warning_only_for_local_daemon(tmp_path, capsys, daemon_host: str, warned: bool) -> None:
    sysctl = tmp_path / "ip_local_reserved_ports"
    sysctl.write_text("\n", encoding="ascii")

    warn_if_host_ports_unreserved(daemon_host, path=str(sysctl))

    assert ("address already in use" in capsys.readouterr().out) is warned


def test_host_port_warning_preserves_existing_reservations(tmp_path, capsys) -> None:
    sysctl = tmp_path / "ip_local_reserved_ports"
    sysctl.write_text("8080,45000-45010\n", encoding="ascii")

    warn_if_host_ports_unreserved(path=str(sysctl))

    assert "net.ipv4.ip_local_reserved_ports=8080,45000-45010,37127-37260" in capsys.readouterr().out
