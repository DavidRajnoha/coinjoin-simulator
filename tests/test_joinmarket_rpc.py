"""jmwalletd transport contracts."""

from unittest.mock import Mock, patch

import pytest

from manager.exceptions import RpcError
from manager.wasabi_clients.joinmarket_clients.joinmarket_client_base import (
    JoinMarketClientServer,
    JoinmarketServiceStateError,
)

REQUEST = "manager.wasabi_clients.joinmarket_clients.joinmarket_client_base.requests.request"
SLEEP = "manager.wasabi_clients.joinmarket_clients.joinmarket_client_base.sleep"


def client() -> JoinMarketClientServer:
    instance = object.__new__(JoinMarketClientServer)
    instance.name = "jcs-000"
    instance.host = "jcs-000"
    instance.port = 28183
    instance.proxy = None
    instance.token = "token"
    instance.walletname = "wallet"
    instance.unlock_wallet = Mock()
    return instance


def unauthorized() -> Mock:
    response = Mock()
    response.status_code = 401
    response.headers = {"WWW-Authenticate": 'Bearer, error="invalid_token"'}
    response.text = ""
    return response


def service_state_401(message: str) -> Mock:
    response = Mock()
    response.status_code = 401
    response.headers = {"Content-Type": "application/json"}
    response.text = f'{{"message": "{message}"}}'
    response.json.return_value = {"message": message}
    return response


def test_a_wallet_that_never_unlocks_fails_instead_of_returning_the_401_body() -> None:
    instance = client()

    with patch(
        "manager.wasabi_clients.joinmarket_clients.joinmarket_client_base.requests.request",
        return_value=unauthorized(),
    ):
        with patch("manager.wasabi_clients.joinmarket_clients.joinmarket_client_base.sleep"):
            with pytest.raises(RpcError, match="stayed unauthorized after 2 attempts"):
                instance._rpc("GET", "/wallet/all", repeat=2)  # pylint: disable=protected-access

    assert instance.unlock_wallet.call_count == 2


def test_a_service_state_401_is_not_an_auth_failure_and_is_not_retried() -> None:
    instance = client()

    with patch(REQUEST, return_value=service_state_401("Service cannot be stopped as it is not running.")) as request:
        with patch(SLEEP):
            with pytest.raises(JoinmarketServiceStateError, match="not running"):
                instance._rpc("GET", "/wallet/wallet/taker/stop")  # pylint: disable=protected-access

    assert request.call_count == 1
    instance.unlock_wallet.assert_not_called()


def test_stopping_an_idle_taker_or_maker_is_a_finished_stop() -> None:
    instance = client()

    with patch(REQUEST, return_value=service_state_401("Service cannot be stopped as it is not running.")):
        assert instance.stop_taker() == {}
        assert instance.stop_maker() == {}

    instance.unlock_wallet.assert_not_called()


def test_invalid_credentials_still_unlock_and_retry() -> None:
    instance = client()

    with patch(REQUEST, return_value=service_state_401("Invalid credentials.")):
        with patch(SLEEP):
            with pytest.raises(RpcError, match="stayed unauthorized after 2 attempts"):
                instance._rpc("POST", "/wallet/wallet/unlock", repeat=2)  # pylint: disable=protected-access

    assert instance.unlock_wallet.call_count == 2


def test_the_startup_probe_reports_a_silent_daemon_as_progress(capsys) -> None:
    instance = client()

    with patch(REQUEST, side_effect=ConnectionError("refused")):
        assert instance.probe_session() is None

    out = capsys.readouterr().out
    assert "[RPC ERROR]" not in out
    assert "not ready yet (ConnectionError)" in out
