"""Readiness waits must give up instead of hanging a run forever."""

from unittest.mock import Mock, patch

import pytest

from manager.wasabi_backend import WasabiBackend
from manager.wasabi_clients import WasabiClient
from manager.wasabi_clients.wasabi_client_base import WasabiClientBase


def test_the_backend_readiness_wait_has_a_deadline() -> None:
    backend = WasabiBackend(host="wasabi-backend", port=37127)
    backend._get_status = Mock(side_effect=RuntimeError("connection refused"))  # pylint: disable=protected-access

    with patch("manager.wasabi_backend.monotonic", side_effect=[0.0, 200.0]):
        with pytest.raises(TimeoutError, match="was not ready after 120s"):
            backend.wait_ready()


def test_a_ready_backend_returns_immediately() -> None:
    backend = WasabiBackend(host="wasabi-backend", port=37127)
    backend._get_status = Mock(return_value={})  # pylint: disable=protected-access

    with patch("manager.wasabi_backend.monotonic", side_effect=[0.0, 1.0]):
        backend.wait_ready()


def test_the_client_readiness_wait_has_a_deadline() -> None:
    client = object.__new__(WasabiClientBase)
    client.host = "wasabi-client-000"
    client.port = 37128
    client.get_status = Mock(side_effect=RuntimeError("connection refused"))

    with patch("manager.wasabi_clients.wasabi_client_base.monotonic", side_effect=[0.0, 200.0]):
        with pytest.raises(TimeoutError, match="was not ready after 60s"):
            client.wait_ready(timeout=60)


def wallet_client(*infos: object) -> WasabiClientBase:
    client = object.__new__(WasabiClientBase)
    client.host = "wasabi-client-distributor"
    client.port = 37128
    client.version = "2.6.0"
    client.proxy = None
    client._create_wallet = Mock(return_value=None)  # pylint: disable=protected-access
    client.get_wallet_info = Mock(side_effect=list(infos))
    return client


def test_wait_wallet_holds_until_the_wallet_reports_started() -> None:
    client = wallet_client(
        {"state": "WaitingForInit", "balance": 0},
        {"state": "Starting", "balance": 0},
        {"state": "Started", "balance": 0},
    )

    with patch("manager.wasabi_clients.wasabi_client_base.sleep"):
        assert client.wait_wallet(timeout=30)

    assert client.get_wallet_info.call_count == 3


def test_current_wasabi_client_waits_until_wallet_started() -> None:
    client = WasabiClient("2.6.0")()
    client._create_wallet = Mock(return_value=None)  # pylint: disable=protected-access
    client.get_wallet_info = Mock(side_effect=[
        {"state": "Starting", "balance": 0},
        {"state": "Started", "balance": 0},
    ])

    with patch("manager.wasabi_clients.wasabi_client_v26.sleep"):
        assert client.wait_wallet(timeout=30)

    assert client.get_wallet_info.call_count == 2


def test_wait_wallet_accepts_a_version_without_a_state_field() -> None:
    client = wallet_client({"balance": 0})

    with patch("manager.wasabi_clients.wasabi_client_base.sleep"):
        assert client.wait_wallet(timeout=30)


def test_wait_wallet_gives_up_on_a_wallet_that_never_starts() -> None:
    client = wallet_client(*[{"state": "Starting", "balance": 0}] * 50)

    with patch("manager.wasabi_clients.wasabi_client_base.sleep"):
        with patch("manager.wasabi_clients.wasabi_client_base.time", side_effect=[0.0, 0.0, 1.0, 2.0, 61.0]):
            assert not client.wait_wallet(timeout=60)
