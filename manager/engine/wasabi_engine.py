import json
import multiprocessing
import multiprocessing.pool
import os
import random
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from time import sleep, time
from traceback import print_exception

import requests

from manager.engine.base.manifest import ProducerLabelEvidence
from manager.engine.configuration import ScenarioConfig, WalletConfig, WasabiConfig
from manager.engine.engine_base import EngineBase
from manager.wasabi_backend_factory import (
    BackendArchitecture,
    create_backend,
    create_coordinator,
    detect_backend_architecture,
    get_backend_image_names,
    get_backend_version,
)
from manager.wasabi_backend_protocol import WasabiBackendProtocol
from manager.wasabi_clients import WasabiClient
from manager.wasabi_coordinator_protocol import WasabiCoordinatorProtocol

SUCCESSFUL_BROADCAST_RE = re.compile(
    r"successfully\s+broadcast(?:ed)?\s+(?:the\s+)?coinjoin(?:\s+transaction)?:\s*([0-9a-f]{64})",
    re.IGNORECASE,
)
WASABI_COORDINATOR_LOG_PATH = "/home/wasabi/.walletwasabi/coordinator/Logs.txt"
WASABI_CLIENT_DATA_PATH = "/home/wasabi/.walletwasabi/client/"
WASABI_SETTLEMENT_BLOCKS_AFTER_LIMIT = 3
DEFAULT_DISTRIBUTOR_STARTUP_TIMEOUT = 900
WASABI_COORDINATOR_START_TIMEOUT_SECONDS = 120
WASABI_COORDINATOR_START_ATTEMPTS = 3
WASABI_COORDINATOR_RETRYABLE_STARTUP_FAILURES = {
    "Bitcoin Node is not fully synchronized": "raced a freshly mined block",
    "address already in use": "could not bind port 37128",
}


def coordinator_retry_reason(coordinator_logs):
    """Explain a known transient coordinator startup failure, if present."""
    for marker, reason in WASABI_COORDINATOR_RETRYABLE_STARTUP_FAILURES.items():
        if marker in coordinator_logs:
            return reason
    return None


def successful_broadcast_txids(log_texts: Iterable[str]) -> set[str]:
    """Extract unique normalized transaction ids from Wasabi broadcast records."""
    return {match.group(1).lower() for log_text in log_texts for match in SUCCESSFUL_BROADCAST_RE.finditer(log_text)}


class WasabiEngine(EngineBase):
    def __init__(self, args, driver):
        self.coordinator: WasabiCoordinatorProtocol | None = None
        self.backend: WasabiBackendProtocol | None = None
        self.backend_architecture: BackendArchitecture | None = None
        self.round_ids: set[str] = set()
        super().__init__(args, driver, WASABI_CLIENT_DATA_PATH)

    def default_scenario(self) -> ScenarioConfig:
        return ScenarioConfig(
            name="default",
            rounds=10,  # the number of coinjoins after which the simulation stops (0 for no limit)
            blocks=0,  # the number of mined blocks after which the simulation stops (0 for no limit)
            default_version="2.6.0",
            wallets=[
                WalletConfig(funds=[200000, 50000], wasabi=WasabiConfig(anon_score_target=7)),
                WalletConfig(funds=[3000000], wasabi=WasabiConfig(redcoin_isolation=True)),
                WalletConfig(funds=[1000000, 500000], wasabi=WasabiConfig(skip_rounds=[0, 1, 2])),
                WalletConfig(funds=[3000000, 15000]),
                WalletConfig(funds=[1000000, 500000]),
                WalletConfig(funds=[3000000, 600000]),
            ],
        )

    def determine_backend_architecture(self) -> BackendArchitecture:
        """Determine which backend architecture to use based on scenario versions."""
        return detect_backend_architecture(self.versions)

    def prepare_images(self):
        print("Preparing images")
        self.prepare_image("btc-node")
        self.prepare_client_images()

        self.backend_architecture = self.determine_backend_architecture()
        for image_name in get_backend_image_names(self.backend_architecture):
            base, version = image_name.split(":")
            path = f"./containers/{base}/{version}"
            self.prepare_image(image_name, path)

    def prepare_client_images(self):
        for version in self.versions:
            name = f"wasabi-client:{version}"
            path = f"./containers/wasabi-clients/{version}"
            self.prepare_image(name, path)

    def start_engine_infrastructure(self):
        if self.backend_architecture is None:
            self.backend_architecture = self.determine_backend_architecture()

        self.start_wasabi_backend()

        if self.backend_architecture == BackendArchitecture.SPLIT:
            self.start_wasabi_coordinator()

    def start_wasabi_backend(self):
        """Start the Wasabi backend with the appropriate version."""
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        if self.backend_architecture is None:
            self.backend_architecture = self.determine_backend_architecture()

        version = get_backend_version(self.backend_architecture)

        wasabi_backend_ip, wasabi_backend_ports, route = self.driver.run(
            "wasabi-backend",
            f"{self.args.image_prefix}wasabi-backend:{version}",
            ports={37127: 37127},
            env={
                "WASABI_BIND": "http://0.0.0.0:37127",
                "ADDR_BTC_NODE": self.args.btc_node_ip or self.node.internal_ip,
            },
            cpu=8.0,
            memory=8192,
        )
        sleep(1)

        config_path = f"./containers/wasabi-backend/{version}/WabiSabiConfig.json"
        with open(config_path, "r") as config_file:
            backend_config = json.load(config_file)
        backend_config.update(self.scenario.backend or {})

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            scenario_file = tmp_file.name
            tmp_file.write(json.dumps(backend_config, indent=2).encode())

        try:
            self.driver.upload(
                "wasabi-backend",
                scenario_file,
                "/home/wasabi/.walletwasabi/backend/WabiSabiConfig.json",
            )
        except Exception as e:
            print_exception(e)
            raise

        backend_host, backend_port = self.service_endpoint(
            wasabi_backend_ip, 37127, wasabi_backend_ports, route
        )
        self.backend = create_backend(
            self.backend_architecture,
            host=backend_host,
            port=backend_port,
            internal_ip=wasabi_backend_ip,
            proxy=self.args.proxy,
        )
        self.backend.wait_ready()
        print(f"- started wasabi-backend ({self.backend_architecture.value} architecture)")

    def start_wasabi_coordinator(self):
        """Start the Wasabi coordinator (only for split architecture)."""
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        if self.backend_architecture is None:
            self.backend_architecture = self.determine_backend_architecture()
        version = get_backend_version(self.backend_architecture)
        for attempt in range(1, WASABI_COORDINATOR_START_ATTEMPTS + 1):
            wasabi_coordinator_ip, wasabi_coordinator_ports, route = self.driver.run(
                "wasabi-coordinator",
                f"{self.args.image_prefix}wasabi-coordinator:{version}",
                ports={37128: 37128},
                env={
                    "ADDR_BTC_NODE": self.args.btc_node_ip or self.node.internal_ip,
                    "WASABI_BIND": "http://0.0.0.0:37128",
                },
                cpu=4.0,
                memory=4096,
            )
            sleep(1)

            coordinator_host, coordinator_port = self.service_endpoint(
                wasabi_coordinator_ip, 37128, wasabi_coordinator_ports, route
            )
            self.coordinator = create_coordinator(
                host=coordinator_host,
                port=coordinator_port,
                internal_ip=wasabi_coordinator_ip,
                proxy=self.args.proxy,
            )
            try:
                self.coordinator.wait_ready(timeout=WASABI_COORDINATOR_START_TIMEOUT_SECONDS)
            except TimeoutError as error:
                try:
                    coordinator_logs = self.driver.logs("wasabi-coordinator").strip()
                except Exception as log_error:
                    coordinator_logs = f"<unable to read coordinator logs: {log_error}>"
                retry_reason = coordinator_retry_reason(coordinator_logs)
                if attempt < WASABI_COORDINATOR_START_ATTEMPTS and retry_reason is not None:
                    print(
                        f"[WARNING] wasabi-coordinator {retry_reason} "
                        f"(attempt {attempt}/{WASABI_COORDINATOR_START_ATTEMPTS}); restarting it"
                    )
                    self.driver.stop("wasabi-coordinator")
                    continue
                raise Exception(
                    f"Wasabi coordinator failed to start: {error}\nCoordinator logs:\n{coordinator_logs}"
                ) from error
            print("- started wasabi-coordinator")
            return

    def start_distributor(self):
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        if self.backend is None:
            raise RuntimeError("Wasabi backend is not initialized")

        backend_address = self.backend.internal_ip
        distributor_env = {
            "ADDR_BTC_NODE": self.args.btc_node_ip or self.node.internal_ip,
            "ADDR_WASABI_BACKEND": self.args.wasabi_backend_ip or backend_address,
        }
        if self.backend_architecture == BackendArchitecture.SPLIT:
            if self.coordinator is None:
                raise RuntimeError("Wasabi coordinator is not initialized for the split architecture")
            distributor_env["ADDR_WASABI_COORDINATOR"] = self.coordinator.internal_ip

        distributor_version = self.scenario.distributor_version or self.scenario.default_version
        wasabi_client_distributor_ip, wasabi_client_distributor_ports, route = self.driver.run(
            "wasabi-client-distributor",
            f"{self.args.image_prefix}wasabi-client:{distributor_version}",
            env=distributor_env,
            ports={37128: 37131},
            cpu=1.0,
            memory=2048,
        )

        distributor_host, distributor_port = self.service_endpoint(
            wasabi_client_distributor_ip, 37128, wasabi_client_distributor_ports, route
        )
        self.distributor = self.init_wasabi_client(
            distributor_version,
            distributor_host,
            port=distributor_port,
            name="wasabi-client-distributor",
            delay=(0, 0),
            stop=(0, 0),
            skip_rounds=(),
        )
        timeout = int(getattr(self.args, "distributor_startup_timeout", DEFAULT_DISTRIBUTOR_STARTUP_TIMEOUT))
        if not self.distributor.wait_wallet(timeout=timeout):
            print(f"- could not start distributor (application timeout {timeout} seconds)")
            raise Exception("Could not start distributor")
        print("- started distributor")

    def init_wasabi_client(self, version, ip, port, name, delay, stop, skip_rounds=()):
        return WasabiClient(version)(
            host=ip,
            port=port,
            name=name,
            proxy=self.args.proxy,
            version=version,
            delay=delay,
            stop=stop,
            skip_rounds=skip_rounds,
        )

    def start_client(self, idx: int, wallet: WalletConfig | None = None):
        if wallet is None:
            raise ValueError("wallet parameter is required")
        version = wallet.version or self.scenario.default_version

        wasabi_config = wallet.wasabi
        anon_score_target = (
            wasabi_config.anon_score_target if wasabi_config else self.scenario.default_anon_score_target
        )
        redcoin_isolation = (
            wasabi_config.redcoin_isolation if wasabi_config else self.scenario.default_redcoin_isolation
        )

        if anon_score_target is not None and version < "2.0.3":
            anon_score_target = None
            print(
                f"Anon Score Target is ignored for wallet {idx} as it is curently supported only for version 2.0.3 and newer"
            )

        if redcoin_isolation is not None and version < "2.0.3":
            redcoin_isolation = None
            print(
                f"Redcoin isolation is ignored for wallet {idx} as it is curently supported only for version 2.0.3 and newer"
            )

        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        if self.backend is None:
            raise RuntimeError("Wasabi backend is not initialized")

        backend_address = self.backend.internal_ip

        sleep(random.random() * 3)
        name = f"wasabi-client-{idx:03}"
        optional_env = {
            "ADDR_BTC_NODE": self.args.btc_node_ip or self.node.internal_ip,
            "ADDR_WASABI_BACKEND": self.args.wasabi_backend_ip or backend_address,
            "WASABI_ANON_SCORE_TARGET": (str(anon_score_target) if anon_score_target else None),
            "WASABI_REDCOIN_ISOLATION": (str(redcoin_isolation) if redcoin_isolation else None),
        }

        if self.backend_architecture == BackendArchitecture.SPLIT and self.coordinator is not None:
            optional_env["ADDR_WASABI_COORDINATOR"] = self.coordinator.internal_ip

        # The driver passes the environment straight to the container runtime,
        # which has no representation for an unset value.
        client_env = {key: value for key, value in optional_env.items() if value is not None}

        try:
            ip, manager_ports, route = self.driver.run(
                name,
                f"{self.args.image_prefix}wasabi-client:{version}",
                env=client_env,
                ports={37128: 37132 + idx},
                cpu=(0.3 if version < "2.0.4" else 0.1),
                memory=(1024 if version < "2.0.4" else 768),
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            return None

        delay = (wallet.delay_blocks or 0, wallet.delay_rounds or 0)
        stop = (wallet.stop_blocks or 0, wallet.stop_rounds or 0)
        skip_rounds = wasabi_config.skip_rounds if wasabi_config and wasabi_config.skip_rounds else ()
        client_host, client_port = self.service_endpoint(ip, 37128, manager_ports, route)
        client = self.init_wasabi_client(
            version,
            client_host,
            client_port,
            f"wasabi-client-{idx:03}",
            delay,
            stop,
            skip_rounds,
        )

        start = time()
        if not client.wait_wallet(timeout=120):
            print(f"- could not start {name} (application timeout {time() - start} seconds)")
            return None
        print(f"- started {client.name} (wait took {time() - start} seconds)")
        return client

    def stop_client(self, idx: int):
        self.driver.stop(f"wasabi-client-{idx:03}")

    def store_engine_logs(self, data_path: str) -> ProducerLabelEvidence:
        try:
            if self.backend_architecture == BackendArchitecture.SPLIT:
                self.driver.download(
                    "wasabi-backend",
                    "/home/wasabi/.walletwasabi/backend/",
                    os.path.join(data_path, "wasabi-backend-2.6"),
                )
                print("- stored backend-2.6 logs")

                try:
                    self.driver.download(
                        "wasabi-coordinator",
                        "/home/wasabi/.walletwasabi/coordinator/",
                        os.path.join(data_path, "wasabi-coordinator"),
                    )
                    print("- stored coordinator logs")
                except Exception:
                    print("- could not store coordinator logs")
            else:
                # Store logs from legacy backend
                self.driver.download(
                    "wasabi-backend",
                    "/home/wasabi/.walletwasabi/backend/",
                    os.path.join(data_path, "wasabi-backend"),
                )
                print("- stored backend logs")
        except Exception:
            print("- could not store backend logs")
            return {
                "engine": "wasabi",
                "complete": False,
                "reason": "could not download Wasabi backend logs",
                "positive_rule": "transaction id appears in a successful coordinator broadcast record",
                "sources": [],
            }

        label_root = os.path.join(
            data_path,
            "wasabi-coordinator" if self.backend_architecture == BackendArchitecture.SPLIT else "wasabi-backend",
        )
        log_paths: list[str] = sorted(
            str(path) for path in Path(label_root).rglob("Logs.txt") if path.is_file()
        )
        successful_txids = successful_broadcast_txids(
            Path(path).read_text(encoding="utf-8", errors="replace") for path in log_paths
        )
        return {
            "engine": "wasabi",
            "complete": bool(log_paths),
            "reason": None if log_paths else "captured Wasabi logs contain no Logs.txt source",
            "positive_rule": "transaction id appears in a successful coordinator broadcast record",
            "positive_count": len(successful_txids),
            "sources": [os.path.relpath(path, data_path) for path in log_paths],
        }

    def client_gone_message(self, client, error):
        state = None
        try:
            state = self.driver.container_state(client.name)
        except Exception:  # pylint: disable=broad-exception-caught
            state = None
        where = f"container state: {state}" if state else "container state unknown"
        return (
            f"client {client.name} is unreachable at {client.host}:{client.port} "
            f"({where}). A Wasabi client that hits a fatal error exits 0 and looks "
            f"\"Completed\", so check that client's own log for the last exception "
            f"before it stopped. Underlying error: {error}"
        )

    def start_coinjoin(self, client):
        sleep(random.random() / 10)
        try:
            client.start_coinjoin()
        except requests.exceptions.ConnectionError as error:
            raise RuntimeError(self.client_gone_message(client, error)) from error

    def stop_coinjoin(self, client):
        sleep(random.random() / 10)
        client.stop_coinjoin()

    def _run_limit_reached(self) -> bool:
        """Return whether the refreshed round or block count reached its scenario limit."""
        return (
            self.scenario.rounds > 0 and self.current_round >= self.scenario.rounds
        ) or (
            self.scenario.blocks > 0 and self.current_block >= self.scenario.blocks
        )

    def update_coinjoins(self):
        def start_condition(client):
            if self._run_limit_reached():
                return False
            if client.stop[0] > 0 and self.current_block >= client.stop[0]:
                return False
            if client.stop[1] > 0 and self.current_round >= client.stop[1]:
                return False
            if self.current_block < client.delay[0]:
                return False
            if self.current_round < client.delay[1]:
                return False
            if self.current_round in client.skip_rounds:
                return False
            return True

        start, stop = [], []
        for client in self.clients:
            if start_condition(client):
                start.append(client)
            else:
                stop.append(client)

        with multiprocessing.pool.ThreadPool() as pool:
            pool.starmap(self.start_coinjoin, ((client,) for client in start))

        with multiprocessing.pool.ThreadPool() as pool:
            pool.starmap(self.stop_coinjoin, ((client,) for client in stop))

    def run_engine(self):
        print("Running simulation")
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        initial_block = self.node.get_block_count()
        while not self._run_limit_reached():
            for attempt in range(3):
                try:
                    self.current_round = self._get_current_round()
                    break
                except Exception as e:
                    print("- could not get rounds".ljust(60), end="\r")
                    print(f"Round exception: {e}", file=sys.stderr)
                    if attempt == 2:
                        raise RuntimeError("Could not refresh Wasabi round count after 3 attempts") from e

            for attempt in range(3):
                try:
                    self.current_block = self.node.get_block_count() - initial_block  # type: ignore
                    break
                except Exception as e:
                    print("- could not get blocks".ljust(60), end="\r")
                    print(f"Block exception: {e}", file=sys.stderr)
                    if attempt == 2:
                        raise RuntimeError("Could not refresh Wasabi block count after 3 attempts") from e

            self.update_invoice_payments()
            self.update_coinjoins()
            print(
                f"- coinjoin rounds: {self.current_round} (block {self.current_block})".ljust(60),
                end="\r",
            )
            sleep(1)
        print()
        print("- limit reached")
        self.mine_settlement_blocks()

    def mine_settlement_blocks(self):
        """Mine the confirmations the last broadcast needs before artifacts are captured."""
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        print(f"- mining {WASABI_SETTLEMENT_BLOCKS_AFTER_LIMIT} settlement blocks")
        if not self.node.mine_block(WASABI_SETTLEMENT_BLOCKS_AFTER_LIMIT):
            raise RuntimeError(
                f"Bitcoin node did not mine {WASABI_SETTLEMENT_BLOCKS_AFTER_LIMIT} settlement blocks"
            )

    def _get_current_round(self) -> int:
        if self.backend_architecture == BackendArchitecture.SPLIT:
            return len(
                successful_broadcast_txids([self.driver.peek("wasabi-coordinator", WASABI_COORDINATOR_LOG_PATH)])
            )

        else:
            # In legacy versions, rounds are tracked by the backend
            return sum(
                1
                for _ in self.driver.peek(
                    "wasabi-backend",
                    "/home/wasabi/.walletwasabi/backend/WabiSabi/CoinJoinIdStore.txt",
                ).split("\n")[:-1]
            )
