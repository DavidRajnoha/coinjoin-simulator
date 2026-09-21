import datetime
import json
import math
import multiprocessing
import multiprocessing.pool
import os
import random
import shutil
import time
from time import sleep
from zoneinfo import ZoneInfo

from manager.btc_node import BtcNode
from manager.engine.base.manifest import write_producer_label_manifest
from manager.engine.configuration import FundConfig, ScenarioConfig, WalletConfig
from manager.exceptions import RpcError
from manager.run_timezone import DEFAULT_RUN_TIMEZONE

DISTRIBUTOR_UTXOS = 200
BATCH_SIZE = 5  # smaller batches avoid UTXO race conditions
BTC = 100_000_000
INFRASTRUCTURE_IMAGES = (
    "btc-node",
    "joinmarket-client-server",
    "irc-server",
    "wasabi-client",
    "wasabi-backend",
    "wasabi-coordinator",
)


def positive_int_env(name: str) -> int | None:
    """Read a positive integer id from the environment, ignoring unusable values.

    Root (0) is skipped on purpose: pods declare runAsNonRoot, and a root caller
    can read the shared datadir whatever owns it.
    """
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _has_fidelity_bond(wallet):
    """True when the wallet asks for a fidelity bond (typed scenario model)."""
    bond = (wallet.joinmarket.fidelity_bond if wallet.joinmarket else None) or {}
    return bool(bond.get("enabled", False))


class EngineBase:
    def __init__(self, args, driver, log_src_path):
        self.args = args
        self.driver = driver
        self.log_src_path = log_src_path
        self.scenario: ScenarioConfig = self.default_scenario()
        self.versions = set()
        self.node: BtcNode | None = None
        self.distributor = None
        self.clients = []
        self.invoices = {}
        self.current_block = 0
        self.current_round = 0

    def default_scenario(self) -> ScenarioConfig:
        raise NotImplementedError

    def load_scenario(self):
        if self.args.command == "run" and self.args.scenario:
            self.scenario = ScenarioConfig.from_json_config(self.args.scenario)

        self.scenario.validate_for_engine(getattr(self.args, "engine", "wasabi"))

        self.versions.add(self.scenario.default_version)
        if self.scenario.distributor_version is not None:
            self.versions.add(self.scenario.distributor_version)
        for wallet in self.scenario.wallets:
            if wallet.version is not None:
                self.versions.add(wallet.version)

    def prepare_images(self):
        raise NotImplementedError

    def image_override(self, name: str) -> str:
        return getattr(self.args, f"{name.replace('-', '_')}_image", "")

    def image_ref(self, name: str) -> str:
        """Resolve an exact infrastructure image override before the prefix default."""
        override = self.image_override(name)
        return override or f"{self.args.image_prefix}{name}"

    def local_build_requested(self, name: str) -> bool:
        return name.split(":", 1)[0] in INFRASTRUCTURE_IMAGES and bool(
            getattr(self.args, "coinjoin_infrastructure_local_build", False)
        )

    def prepare_image(self, name: str, path=None, build_args=None):
        image_name = self.image_ref(name)
        has_override = bool(self.image_override(name))
        # Only forward the argument when there is one, so the common build keeps
        # the plain two-argument driver call every driver already implements.
        extra = {"build_args": build_args} if build_args else {}
        if self.local_build_requested(name):
            self.driver.build(
                image_name, f"./containers/{name}" if path is None else path, **extra
            )
            print(f"- image built {image_name}")
        elif self.driver.has_image(image_name):
            if self.args.force_rebuild:
                if self.args.image_prefix or has_override:
                    self.driver.pull(image_name)
                    print(f"- image pulled {image_name}")
                else:
                    self.driver.build(
                        name, f"./containers/{name}" if path is None else path, **extra
                    )
                    print(f"- image rebuilt {image_name}")
            else:
                print(f"- image reused {image_name}")
        elif self.args.image_prefix or has_override:
            self.driver.pull(image_name)
            print(f"- image pulled {image_name}")
        else:
            self.driver.build(
                name, f"./containers/{name}" if path is None else path, **extra
            )
            print(f"- image built {image_name}")

    def service_endpoint(self, ip, container_port, ports, route=None, *, tls=False):
        """Resolve a driver endpoint for local, proxied, in-cluster and routed runs."""
        if self.args.in_cluster:
            # The Kubernetes driver returns a Service DNS name, which is reached
            # on the Service port even through a proxy.
            return ip, ports.get(container_port, container_port)
        if self.args.proxy:
            return ip, container_port
        # OpenShift passthrough routes require TLS at both ends. Plain HTTP
        # services keep their published port; selecting 443 cannot enable TLS.
        if route and tls:
            return str(route), 443
        return self.args.control_ip, ports[container_port]

    def start_infrastructure(self):
        print("Starting infrastructure")
        self.start_btc_node()
        self.start_engine_infrastructure()
        self.start_distributor()

    def start_btc_node(self):
        node_volumes = None
        # bitcoind creates <datadir>/regtest with mode 0700, so the image's own
        # user would leave a shared datadir unreadable for whoever runs the
        # analysis afterwards. Run the node as the caller's storage identity.
        storage_uid = None
        storage_gid = None
        if self.args.btcFolder:
            node_volumes = {os.path.abspath(self.args.btcFolder): {"bind": "/home/bitcoin/data", "mode": "rw"}}
            storage_uid = positive_int_env("KUBERNETES_STORAGE_UID")
            storage_gid = positive_int_env("KUBERNETES_STORAGE_GID") if storage_uid else None
        command = ["./run.sh", *self.args.btc_node_arg] if self.args.btc_node_arg else None
        btc_node_env = {}
        initial_block_count = os.environ.get("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT")
        if initial_block_count:
            btc_node_env["COINJOIN_INITIAL_BLOCK_COUNT"] = initial_block_count

        btc_node_ip, btc_node_ports, route = self.driver.run(
            "btc-node",
            self.image_ref("btc-node"),
            ports={18443: 18443, 18444: 18444},
            cpu=2.0,
            memory=2048,
            service_account="btc-node",
            env=btc_node_env or None,
            volumes=node_volumes,
            command=command,
            run_as_user=storage_uid,
            run_as_group=storage_gid,
        )

        print(btc_node_ip, btc_node_ports)
        node_host, node_port = self.service_endpoint(btc_node_ip, 18443, btc_node_ports, route)
        self.node = BtcNode(
            host=node_host,
            port=node_port,
            internal_ip=btc_node_ip,
            proxy=self.args.proxy,
        )
        print("BTC node startup in progress")
        self.node.wait_ready()
        print("- started btc-node")

    def start_engine_infrastructure(self):
        raise NotImplementedError

    def start_distributor(self):
        raise NotImplementedError

    def init_client(self):
        raise NotImplementedError

    def start_client(self, idx: int, wallet=None):
        raise NotImplementedError

    def stop_client(self, idx: int):
        raise NotImplementedError

    def _start_classified_wallets(self, pool, wallet_list, fb_batch_size=5, fb_batch_delay=15):
        """
        Start a list of (idx, wallet) tuples with smart batching:
        - Regular wallets: all in parallel
        - FB wallets: in small batches to avoid Bitcoin RPC overload

        Returns dict: {idx: client} (client is None if startup failed)
        """
        # Classify into regular and FB wallets
        regular_wallets = []
        fb_wallets = []

        for idx, wallet in wallet_list:
            if _has_fidelity_bond(wallet):
                fb_wallets.append((idx, wallet))
            else:
                regular_wallets.append((idx, wallet))

        results = {}

        # Start regular wallets in parallel (fast)
        if regular_wallets:
            regular_clients = pool.starmap(self.start_client, regular_wallets)
            for (idx, _), client in zip(regular_wallets, regular_clients):
                results[idx] = client

        # Start FB wallets in batches (slow, avoid RPC overload)
        if fb_wallets:
            for batch_start in range(0, len(fb_wallets), fb_batch_size):
                batch_end = min(batch_start + fb_batch_size, len(fb_wallets))
                batch = fb_wallets[batch_start:batch_end]

                batch_clients = pool.starmap(self.start_client, batch)
                for (idx, _), client in zip(batch, batch_clients):
                    results[idx] = client

                # Wait before next batch (unless last batch)
                if batch_end < len(fb_wallets) and fb_batch_delay > 0:
                    print(f"  - waiting {fb_batch_delay}s before next batch")
                    sleep(fb_batch_delay)

        return results

    def start_clients(self, wallets):
        print("Starting clients")

        fb_batch_size = 5   # FB wallets per batch
        fb_batch_delay = 15  # Seconds between FB batches

        # Build initial wallet list with indices
        wallet_list = [(idx, wallet) for idx, wallet in enumerate(wallets, start=len(self.clients))]

        # Count wallet types for logging
        fb_count = sum(1 for _, w in wallet_list if _has_fidelity_bond(w))
        print(f"- {len(wallet_list) - fb_count} regular wallets, {fb_count} fidelity bond wallets")

        with multiprocessing.pool.ThreadPool() as pool:
            new_clients = [None] * len(wallets)

            # Initial startup
            print(f"- starting {len(wallet_list)} wallets")
            if fb_count > 0:
                print(f"  - FB wallets will be started in batches of {fb_batch_size}")

            startup_results = self._start_classified_wallets(pool, wallet_list, fb_batch_size, fb_batch_delay)
            for idx, client in startup_results.items():
                new_clients[idx - len(self.clients)] = client

            # Retry logic (uses same classification/batching)
            for retry_attempt in range(3):
                failed_indices = [
                    idx for idx, client in enumerate(new_clients, start=len(self.clients))
                    if client is None or not self._client_is_healthy(client)
                ]

                if not failed_indices:
                    break

                print(f"- failed to start {len(failed_indices)} clients; retrying ...")

                # Stop and rebuild wallet list for failed clients
                for idx in failed_indices:
                    self.stop_client(idx)
                sleep(60)

                retry_wallet_list = [(idx, wallets[idx - len(self.clients)]) for idx in failed_indices]

                # Retry with same smart batching
                retry_results = self._start_classified_wallets(pool, retry_wallet_list, fb_batch_size, fb_batch_delay)
                for idx, client in retry_results.items():
                    new_clients[idx - len(self.clients)] = client

            failed_count = sum(client is None for client in new_clients)
            if failed_count:
                raise RuntimeError(
                    f"Failed to start {failed_count} clients after retries; aborting experiment"
                )

        self.clients.extend(new_clients)

    @staticmethod
    def _client_health_error(client):
        """Probe an already-created wallet without mutating it.

        wait_wallet() is not read-only for JoinMarket: it creates the wallet
        again and can loop on "Wallet already unlocked" until the timeout.
        """
        try:
            client.get_balance()
        except Exception as error:  # pylint: disable=broad-exception-caught
            return str(error)
        return None

    @classmethod
    def _client_is_healthy(cls, client):
        return cls._client_health_error(client) is None

    def validate_clients(self):
        """Require every declared wallet to answer before funding begins."""
        expected = len(self.scenario.wallets)
        actual = len(self.clients)
        if actual != expected:
            raise RuntimeError(f"Expected {expected} clients, but only {actual} started")

        def healthcheck(client):
            detail = self._client_health_error(client)
            return client.name, detail is None, detail

        with multiprocessing.pool.ThreadPool() as pool:
            results = pool.map(healthcheck, self.clients)
        failed = [
            f"{name} ({detail or 'RPC health-check failed'})"
            for name, healthy, detail in results
            if not healthy
        ]
        if failed:
            raise RuntimeError("Client RPC health-check failed before funding: " + ", ".join(failed))

    def fund_distributor(self, btc_amount):
        print("Funding distributor")
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        if self.distributor is None:
            raise RuntimeError("Distributor is not initialized")

        # Round each UTXO up to a whole satoshi so the total meets the target
        # even when the amount does not divide evenly. Integer-dividing by BTC
        # here would fund every address with 0 BTC for any total below 200 BTC.
        per_utxo_sats = math.ceil(btc_amount * BTC / DISTRIBUTOR_UTXOS)
        for _ in range(DISTRIBUTOR_UTXOS):
            self.node.fund_address(
                self.distributor.get_new_address(),
                # Bitcoin Core rejects scientific notation, so keep 8 decimals.
                float(f"{per_utxo_sats / BTC:.8f}"),
            )

        while (balance := self.distributor.get_balance()) < btc_amount * BTC:
            sleep(1)
        print(f"- funded (current balance {balance / BTC:.8f} BTC)")

    def store_client_logs(self, client, data_path):
        sleep(random.random() * 3)
        client_path = os.path.join(data_path, client.name)
        os.mkdir(client_path)
        with open(os.path.join(client_path, "coins.json"), "w") as f:
            json.dump(client.list_coins(), f, indent=2)
            print(f"- stored {client.name} coins")
        with open(os.path.join(client_path, "unspent_coins.json"), "w") as f:
            json.dump(client.list_unspent_coins(), f, indent=2)
            print(f"- stored {client.name} unspent coins")
        with open(os.path.join(client_path, "keys.json"), "w") as f:
            json.dump(client.list_keys(), f, indent=2)
            print(f"- stored {client.name} keys")

        # Store fidelity bonds data if available
        if hasattr(client, 'export_fidelity_bonds_data') and hasattr(client, 'fidelity_bonds'):
            bonds_data = client.export_fidelity_bonds_data(self.current_block)
            with open(os.path.join(client_path, "fidelity_bonds.json"), "w") as f:
                json.dump(bonds_data, f, indent=2)
                if bonds_data['total_bonds'] > 0:
                    print(f"- stored {client.name} fidelity bonds ({bonds_data['total_bonds']} bonds)")
                else:
                    print(f"- stored {client.name} fidelity bonds (none)")

        try:
            self.driver.download(client.name, self.log_src_path, client_path)
            print(f"- stored {client.name} logs, {self.log_src_path}, {client_path}")
        except Exception as e:
            print(f"- could not store {client.name} logs: {e!r}")

    def log_run_path(self):
        requested_run_id = getattr(self.args, "run_id", "")
        if requested_run_id:
            return f"./logs/{requested_run_id}"
        run_timezone = getattr(self.args, "run_timezone", DEFAULT_RUN_TIMEZONE)
        timestamp = datetime.datetime.now(ZoneInfo(run_timezone)).strftime("%Y-%m-%d_%H-%M")
        return f"./logs/{timestamp}_{self.scenario.name}"

    def ensure_log_run_path_available(self) -> None:
        # The pipeline launcher may pre-create the run directory to place its
        # host manifest there; only a directory that already holds emulator
        # artifacts marks a genuine earlier run.
        run_path = self.log_run_path()
        if os.path.exists(os.path.join(run_path, "coinjoin_emulator_data")):
            raise RuntimeError(f"Run log directory already exists: {run_path}")

    def store_logs(self):
        print("Storing logs")
        run_path = self.log_run_path()
        experiment_path = os.path.join(run_path, "coinjoin_emulator_data")
        data_path = os.path.join(experiment_path, "data")
        os.makedirs(data_path)

        with open(os.path.join(experiment_path, "scenario.json"), "w") as f:
            json.dump(self.scenario.to_dict(), f, indent=2)
            print("- stored scenario")

        stored_blocks = 0
        node_path = os.path.join(data_path, "btc-node")
        os.mkdir(node_path)
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        try:
            block_count = self.node.get_block_count()
        except (TypeError, RpcError) as error:
            # Only the block count is optional: without it there is nothing to export.
            print(f"Failed to get block count: {error}")
        else:
            while stored_blocks <= block_count:
                block_hash = self.node.get_block_hash(stored_blocks)
                block = self.node.get_block_info(block_hash)
                with open(os.path.join(node_path, f"block_{stored_blocks}.json"), "w") as f:
                    json.dump(block, f, indent=2)
                stored_blocks += 1

        print(f"- stored {stored_blocks} blocks")

        print("- storing engine logs")
        producer_label_evidence = self.store_engine_logs(data_path)
        write_producer_label_manifest(data_path, producer_label_evidence)
        print("- finished storing engine logs, stored producer-label manifest")

        print(f"- storing logs for {len(self.clients)} clients in parallel")
        with multiprocessing.pool.ThreadPool() as pool:
            pool.starmap(self.store_client_logs, [(client, data_path) for client in self.clients])

        archive_base = os.path.join(run_path, ".emulation_logs")
        archive_path = shutil.make_archive(archive_base, "zip", run_path, "coinjoin_emulator_data")
        os.replace(archive_path, os.path.join(experiment_path, "emulation_logs.zip"))
        print("- zip archive created")

    def store_engine_logs(self, data_path: str) -> dict[str, object] | None:
        print("Storing engine logs / NOT IMPLEMENTED")
        raise NotImplementedError

    def stop_coinjoins(self):
        print("Stopping coinjoins")
        
        # Helper function to stop a single client's coinjoin
        def stop_single_client(client):
            try:
                client.stop_coinjoin()
                print(f"- stopped mixing {client.name}")
                return True
            except Exception as e:
                print(f"- could not stop mixing {client.name}: {e}")
                return False
        
        # Use ThreadPool to parallelize stopping coinjoins
        with multiprocessing.pool.ThreadPool() as pool:
            results = pool.map(stop_single_client, self.clients)
            
        success_count = sum(1 for r in results if r)
        print(f"- stopped mixing for {success_count}/{len(self.clients)} clients")

    def shutdown_engine(self):
        """Release engine-specific local resources after the clients have stopped."""

    def update_invoice_payments(self):
        due = list(filter(lambda x: x[0] <= self.current_block and x[1] <= self.current_round, self.invoices.keys()))
        for schedule in due:
            # Keep the invoices scheduled until payment succeeds. A transient
            # RPC failure must not silently erase wallet funding.
            self.pay_invoices(self.invoices[schedule])
            del self.invoices[schedule]

    def prepare_invoices(self, wallets: list[WalletConfig]):
        print("Preparing invoices")
        client_invoices = [(client, wallet.funds) for client, wallet in zip(self.clients, wallets)]

        for client, funds in client_invoices:
            for fund in funds:
                block = 0
                round = 0
                if isinstance(fund, int):
                    value = fund
                elif isinstance(fund, FundConfig):
                    value = fund.value
                    block = fund.delay_blocks or 0
                    round = fund.delay_rounds or 0
                else:
                    raise TypeError(f"unsupported fund entry for {client.name}: {fund!r}")
                addressed_invoice = (client.get_new_address(), value)
                if (block, round) not in self.invoices:
                    self.invoices[(block, round)] = [addressed_invoice]
                else:
                    self.invoices[(block, round)].append(addressed_invoice)

        for addressed_invoices in self.invoices.values():
            random.shuffle(addressed_invoices)

        print(f"- prepared {sum(map(len, self.invoices.values()))} invoices")

    def pay_invoices(self, addressed_invoices):
        print(
            f"- paying {len(addressed_invoices)} invoices (batch size {BATCH_SIZE}, block {self.current_block}, round {self.current_round})"
        )
        try:
            while addressed_invoices:
                batch = addressed_invoices[:BATCH_SIZE]
                for _ in range(3):
                    try:
                        if self.distributor is None:
                            raise RuntimeError("Distributor is not initialized")
                        result = self.distributor.send(batch)
                        if str(result) == "timeout":
                            print("- transaction timeout")
                            continue
                        # Retain only unpaid batches if a later send fails.
                        del addressed_invoices[:len(batch)]
                        break
                    except Exception as e:
                        # https://github.com/zkSNACKs/WalletWasabi/issues/12764
                        if "Bad Request" in str(e):
                            print("- transaction error (bad request)")
                        else:
                            print(f"- transaction error ({e})")
                else:
                    print("- invoice payment failed")
                    raise Exception("Invoice payment failed")

        except Exception as e:
            print("- invoice payment failed")
            raise e

    def prepare_additional_funding(self, wallets):
        """
        Hook for engines to perform additional post-funding setup.
        Default implementation does nothing.

        Args:
            wallets: List of wallet configurations
        """
        pass
    
    def run(self):
        print(f"=== Scenario {self.scenario.name} ===")
        if not getattr(self.args, "no_logs", False):
            self.ensure_log_run_path_available()
        self.prepare_images()
        self.start_infrastructure()
        self.fund_distributor(5000)
        self.start_clients(self.scenario.wallets)
        self.validate_clients()
        time.sleep(60)
        self.prepare_invoices(self.scenario.wallets)

        # Pay initial wallet funding invoices before additional funding
        print("Paying initial wallet funding")
        self.update_invoice_payments()

        # Allow engines to perform additional post-funding setup (e.g., fidelity bonds)
        self.prepare_additional_funding(self.scenario.wallets)

        print("Running simulation")
        self.run_engine()

    def run_engine(self):
        raise NotImplementedError
