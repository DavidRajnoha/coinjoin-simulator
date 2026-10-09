from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from manager.exceptions import CoinjoinEmulatorError
from manager.kubernetes_local_proxy import KubernetesLocalProxy


def proxy() -> KubernetesLocalProxy:
    instance = object.__new__(KubernetesLocalProxy)
    instance._kubectl_base_cmd = ["kubectl"]  # pylint: disable=protected-access
    instance.namespace = "coinjoin"
    instance.orchestrator_pod = "deployment/emulation-manager"
    return instance


@pytest.fixture
def manifest_path(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "containers/emulator-manager/deployment.yaml"
    destination = tmp_path / "containers/emulator-manager/deployment.yaml"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    monkeypatch.chdir(tmp_path)
    return destination


@pytest.mark.parametrize("already_matches", [False, True])
def test_override_preserves_manifest_and_applies_complete_deployment_once(manifest_path, already_matches):
    original = yaml.safe_load(manifest_path.read_text())
    containers = original["spec"]["template"]["spec"]["containers"]
    if already_matches:
        containers[0]["image"] = "registry.example/emulator-manager"
    containers.insert(0, {"name": "sidecar", "image": "sidecar:1"})
    manifest_path.write_text(yaml.safe_dump(original))
    original_bytes = manifest_path.read_bytes()
    expected = deepcopy(original)
    expected["spec"]["template"]["spec"]["containers"][1]["image"] = "registry.example/emulator-manager"
    for init_container in expected["spec"]["template"]["spec"].get("initContainers", []):
        if init_container["image"] == containers[1]["image"]:
            init_container["image"] = "registry.example/emulator-manager"

    with patch("manager.kubernetes_local_proxy.subprocess.run") as run:
        assert proxy().deploy_manager(image_prefix="registry.example/", wait_ready=False)

    run.assert_called_once()
    assert run.call_args.args[0] == ["kubectl", "apply", "-f", "-", "-n", "coinjoin"]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["text"] is True
    assert yaml.safe_load(run.call_args.kwargs["input"]) == expected
    assert manifest_path.read_bytes() == original_bytes


def test_missing_manager_stops_before_deployment_submission(manifest_path):
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["spec"]["template"]["spec"]["containers"][0]["name"] = "other"
    manifest_path.write_text(yaml.safe_dump(manifest))

    with patch("manager.kubernetes_local_proxy.subprocess.run") as run:
        with pytest.raises(CoinjoinEmulatorError, match="Container 'manager' not found"):
            proxy().deploy_manager(image_prefix="registry.example/", wait_ready=False)
    run.assert_not_called()


def test_deploy_manager_applies_the_original_deployment_without_an_override(manifest_path):
    original_bytes = manifest_path.read_bytes()
    with patch("manager.kubernetes_local_proxy.subprocess.run") as run:
        assert proxy().deploy_manager(wait_ready=False)

    run.assert_called_once_with(
        [
            "kubectl", "apply", "-f",
            "./containers/emulator-manager/deployment.yaml", "-n", "coinjoin",
        ],
        check=True,
    )
    assert manifest_path.read_bytes() == original_bytes
