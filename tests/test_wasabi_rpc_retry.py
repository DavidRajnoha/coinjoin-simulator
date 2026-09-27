"""Retry behaviour of the Wasabi client RPC."""

from unittest.mock import Mock, patch

import pytest
import requests

from manager.exceptions import RpcError
from manager.wasabi_clients.wasabi_client_base import WasabiClientBase


def client() -> WasabiClientBase:
    return WasabiClientBase(name="wasabi-client-distributor", host="host", port=37131)


def ok(body):
    result = Mock()
    result.json.return_value = body
    return result


def test_a_new_address_survives_one_connect_timeout() -> None:
    """fund_distributor asks 200 times in a row; one blip must not end the run."""
    answers = [
        requests.exceptions.ConnectTimeout("connect timed out"),
        ok({"result": {"address": "bcrt1qexample"}}),
    ]
    with patch("manager.wasabi_clients.wasabi_client_base.requests.post", side_effect=answers), \
         patch("manager.wasabi_clients.wasabi_client_base.sleep") as slept:
        assert client().get_new_address() == "bcrt1qexample"

    slept.assert_called_once()


def test_retries_are_spread_out_but_not_after_the_last_attempt() -> None:
    """Immediate retries land in the same stall, and a trailing sleep is waste."""
    timeout = requests.exceptions.ConnectTimeout("connect timed out")
    with patch(
        "manager.wasabi_clients.wasabi_client_base.requests.post",
        side_effect=[timeout, timeout, timeout],
    ), patch("manager.wasabi_clients.wasabi_client_base.sleep") as slept:
        with pytest.raises(requests.exceptions.ConnectTimeout):
            client().get_new_address()

    assert slept.call_count == 2


def test_an_rpc_error_is_not_retried() -> None:
    """Only timeouts are transient; a refusal from the wallet is an answer."""
    with patch(
        "manager.wasabi_clients.wasabi_client_base.requests.post",
        return_value=ok({"error": "no such wallet"}),
    ) as post:
        with pytest.raises(Exception):
            client().get_new_address()

    assert post.call_count == 1


RACE = {
    "code": -32603,
    "message": "Destination array was not long enough. Check the destination index, "
    "length, and the array's lower bounds. (Parameter 'destinationArray')",
}


def test_a_new_address_survives_the_key_cache_race() -> None:
    """Wasabi 2.6.0 races its synchronizer on the key cache; the retry gets the address."""
    with patch(
        "manager.wasabi_clients.wasabi_client_base.requests.post",
        side_effect=[ok({"error": RACE}), ok({"result": {"address": "bcrt1qexample"}})],
    ), patch("manager.wasabi_clients.wasabi_client_base.sleep") as slept:
        assert client().get_new_address() == "bcrt1qexample"

    slept.assert_called_once()


def test_a_persisting_key_cache_race_still_fails() -> None:
    with patch(
        "manager.wasabi_clients.wasabi_client_base.requests.post",
        return_value=ok({"error": RACE}),
    ) as post, patch("manager.wasabi_clients.wasabi_client_base.sleep"):
        with pytest.raises(RpcError):
            client().get_new_address()

    assert post.call_count == 3


def test_other_internal_errors_are_not_retried() -> None:
    """-32603 also carries deterministic failures such as an unloaded wallet."""
    with patch(
        "manager.wasabi_clients.wasabi_client_base.requests.post",
        return_value=ok({"error": {"code": -32603, "message": "There is no wallet loaded."}}),
    ) as post:
        with pytest.raises(RpcError):
            client().get_new_address()

    assert post.call_count == 1
