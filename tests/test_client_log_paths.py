"""Each engine downloads client logs from the directory its client pods actually write."""

from types import SimpleNamespace

from manager.engine.joinmarket_engine import JoinmarketEngine
from manager.engine.wasabi_engine import WasabiEngine


def test_wasabi_clients_store_the_client_directory_not_the_backend_one() -> None:
    engine = WasabiEngine(SimpleNamespace(), driver=None)

    # The backend/ tree exists only in the backend pod; every client download
    # from it failed with "tar: backend: Cannot stat" until 2026-09-20.
    assert engine.log_src_path == "/home/wasabi/.walletwasabi/client/"


def test_joinmarket_clients_store_the_daemon_log_directory() -> None:
    engine = JoinmarketEngine(SimpleNamespace(), driver=None)

    assert engine.log_src_path == "/home/joinmarket/.joinmarket/logs"
