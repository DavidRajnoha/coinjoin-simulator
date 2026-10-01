import backoff
import asyncio
import json
import random

from manager.engine.engine_base import EngineBase
from manager.wasabi_clients.joinmarket_clients.joinmarket_client_base import JoinMarketClientServer
from manager.wasabi_clients.joinmarket_clients.joinmarket_clients import OrderbookWatchClient
from time import sleep, time
import os
import shutil
SCENARIO = {
    "name": "default",
    "default_version": "joinmarket",  # "joinmarket" (reference client-server) or "joinmarket-ng"
    "rounds": 0,  # the number of coinjoins after which the simulation stops (0 for no limit)
    "blocks": 0,  # the number of mined blocks after which the simulation stops (0 for no limit)
    "wallets": [],
}
import sys

NG_VERSION = "joinmarket-ng"

# joinmarket-ng directory server (the message channel shared by NG and, in mixed runs, reference clients)
NG_DIRECTORY_NAME = "jm-directory"
NG_DIRECTORY_PORT = 5222
NG_DIRECTORY_NODES = f"{NG_DIRECTORY_NAME}:{NG_DIRECTORY_PORT}"
NG_DIRECTORY_ID = f"test:{NG_DIRECTORY_NAME}-{NG_DIRECTORY_PORT}"
NG_NICK_AUTH_IDS = json.dumps({NG_DIRECTORY_NODES: NG_DIRECTORY_ID})
NG_OBWATCH_PORT = 8000

NG_CLIENT_ENV = {
    "BITCOIN__BACKEND_TYPE": "descriptor_wallet",
    "BITCOIN__RPC_URL": "http://btc-node:18443",
    "BITCOIN__RPC_USER": "user",
    "BITCOIN__RPC_PASSWORD": "password",
    # reference JoinMarket announces regtest as "testnet" on the wire; NG follows suit
    "NETWORK_CONFIG__NETWORK": "testnet",
    "NETWORK_CONFIG__BITCOIN_NETWORK": "regtest",
    "NETWORK_CONFIG__ALLOW_CLEARNET_CONNECTIONS": "true",
    "NETWORK_CONFIG__DIRECTORY_SERVERS": NG_DIRECTORY_NODES,
    "NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS": NG_NICK_AUTH_IDS,
    # simulated makers charge fees without fidelity bonds
    "TAKER__BONDLESS_REQUIRE_ZERO_FEE": "false",
    "TAKER__BONDLESS_MAKERS_ALLOWANCE": "0.125",
    # Parity with the tuned reference joinmarket.cfg / plain yield generator, so NG takers see the
    # same maker set as reference takers (NG defaults: 500 sats / 0.1% / public fee grid only,
    # at most 15 inputs per maker, offers withdrawn for up to 600 s after every CoinJoin).
    "TAKER__MAX_CJ_FEE_ABS": "100000",
    "TAKER__MAX_CJ_FEE_REL": "0.01",
    "TAKER__REQUIRE_QUANTIZED_CJ_FEES": "false",
    # must stay > 0: sweeps size their miner fee from this cap before maker inputs are known
    "TAKER__MAX_MAKER_UTXOS": "50",
    "TAKER__MINIMUM_MAKERS": "4",
    "TAKER__MAKER_TIMEOUT_SEC": "60",
    "TAKER__ORDERBOOK_MIN_WAIT": "20",
    "TAKER__TAKER_UTXO_AGE": "5",
    "MAKER__OFFER_REANNOUNCE_DELAY_MAX": "0",
    "MAKER__SIZE_FACTOR": "0",
    "MAKER__MIN_CONFIRMATIONS": "1",
    # the emulator funds makers into mixdepth 0 only; without merging, maxsize = largest single UTXO
    "MAKER__ALLOW_MIXDEPTH_ZERO_MERGE": "true",
    # history.csv confirmations are written by the bots' rescans (default 600 s)
    "MAKER__RESCAN_INTERVAL_SEC": "60",
    "TAKER__RESCAN_INTERVAL_SEC": "60",
    # regtest pacing for the tumbler runner (production defaults wait 30 min / 6 confirmations);
    # 5 confirmations between phases = taker_utxo_age, so the next phase never fails on PoDLE age
    "TUMBLER__RETRY_DELAY_SECONDS": "30",
    "TUMBLER__CONFIRMATION_POLL_INTERVAL": "5",
    "TUMBLER__MIN_CONFIRMATIONS_BETWEEN_PHASES": "5",
    "LOGGING__LEVEL": "DEBUG",
    "LOGGING__SENSITIVE": "true",
    "JMWALLETD_HOST": "0.0.0.0",
}
# privacy-enhanced offer factors are daemon settings in NG, not maker/start fields
NG_OFFER_FACTOR_ENV = {
    "txfee_factor": "MAKER__TXFEE_CONTRIBUTION_FACTOR",
    "cjfee_factor": "MAKER__CJFEE_FACTOR",
    "size_factor": "MAKER__SIZE_FACTOR",
}



class JoinmarketEngine(EngineBase):

    def __init__(self, args, driver):
        super().__init__(args, driver,
                         log_src_path="/home/joinmarket/.joinmarket/logs")
        self.obwatch_client = None
        # Feature flag to enable async client updates (default: enabled for better performance)
        self.async_updates = getattr(args, 'async_updates', True)
        self.loop = None
        self.last_resource_check = 0  # Track when we last checked resources

    def default_scenario(self):
        return SCENARIO

    def wallet_version(self, wallet) -> str:
        return wallet.get("version", self.scenario["default_version"])

    @property
    def directory_mode(self) -> bool:
        """True when any joinmarket-ng wallet runs: everyone then talks over the NG directory server."""
        return NG_VERSION in self.versions

    def prepare_images(self):
        print("Preparing images")
        self.prepare_image("btc-node")
        self.prepare_image("joinmarket-client-server")
        if self.directory_mode:
            self.prepare_image("joinmarket-ng")
            self.prepare_image("joinmarket-ng-directory")
            self.prepare_image("joinmarket-ng-obwatcher")
        else:
            self.prepare_image("irc-server")


    def start_engine_infrastructure(self):
        self.node.create_wallet("jm_wallet")
        print("- created jm_wallet in BitcoinCore")

        if self.directory_mode:
            self.start_directory_server()
            print("- started joinmarket-ng directory server")
            try:
                self.start_ng_orderbook_watch()
                print("- started joinmarket-ng orderbook watcher")
            except Exception as e:
                print(f"- could not start orderbook watcher ({e})")
            return

        self.start_irc_server()
        print("- started irc-server")

        # Start the JoinMarket orderbook watcher service and attach a client to poll it
        try:
            self.start_orderbook_watch()
            print("- started orderbook watcher")
        except Exception as e:
            print(f"- could not start orderbook watcher ({e})")

    def start_directory_server(self):
        try:
            self.driver.run(
                NG_DIRECTORY_NAME,
                f"{self.args.image_prefix}joinmarket-ng-directory",
                env={
                    "NETWORK_CONFIG__NETWORK": "testnet",
                    "DIRECTORY_SERVER__HOST": "0.0.0.0",
                    "DIRECTORY_SERVER__PORT": str(NG_DIRECTORY_PORT),
                    "DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID": NG_DIRECTORY_ID,
                    "LOGGING__LEVEL": "INFO",
                },
                ports={NG_DIRECTORY_PORT: NG_DIRECTORY_PORT},
                cpu=0.5,
                memory=512,
                service_account="joinmarket",
                run_as_user=1000,
                run_as_group=1000,
            )
        except Exception as e:
            print(f"- could not start {NG_DIRECTORY_NAME} ({e})")
            raise Exception("Could not start joinmarket-ng directory server")

    def start_ng_orderbook_watch(self):
        name = "joinmarket-obwatch"
        port = NG_OBWATCH_PORT
        try:
            ip, obwatch_ports, route = self.driver.run(
                name,
                f"{self.args.image_prefix}joinmarket-ng-obwatcher",
                env={
                    "NETWORK_CONFIG__NETWORK": "testnet",
                    "NETWORK_CONFIG__ALLOW_CLEARNET_CONNECTIONS": "true",
                    "DIRECTORY_NODES": NG_DIRECTORY_NODES,
                    "NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS": NG_NICK_AUTH_IDS,
                    "ORDERBOOK_WATCHER__HTTP_HOST": "0.0.0.0",
                    "ORDERBOOK_WATCHER__HTTP_PORT": str(port),
                    "LOGGING__LEVEL": "INFO",
                },
                ports={port: port},
                cpu=0.25,
                memory=256,
                service_account="joinmarket",
                run_as_user=1000,
                run_as_group=1000,
                proxy=self.args.proxy
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            raise Exception("Could not start joinmarket-ng orderbook watcher")

        actual_port = port if self.args.proxy else (443 if route else obwatch_ports[port])
        actual_ip = ip if self.args.proxy or self.args.in_cluster else (route if route else self.args.control_ip)
        print(f"- started {name} at {actual_ip}:{actual_port}")

        self.obwatch_client = OrderbookWatchClient(
            name=name,
            host=actual_ip,
            port=actual_port,
            type="orderbook",
            supports_refresh=False,
            supports_bonds=False,
        )


    def start_irc_server(self):
        # TODO: When the container fails to start, the exception is not thrown and it is not recognized.
        name = "irc-server"

        try:
            ip, manager_ports, _ = self.driver.run(
                name,
                f"{self.args.image_prefix}irc-server",
                env={},  # Add any necessary environment variables
                ports={6667: 6667},
                cpu=0.25,
                memory=256,
                service_account="irc-server",
                run_as_user=10000,
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            raise Exception("Could not start IRC server")


    def start_distributor(self):
        name = "joinmarket-distributor"
        port = 28183  # Use a specific port for the distributor
        try:
            ip, distributor_node_ports, route = self.driver.run(
                name,
                f"{self.args.image_prefix}joinmarket-client-server",
                env=self._reference_client_env(),
                ports={28183: port},
                cpu=1,
                memory=1024,
                service_account="joinmarket"
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            raise Exception("Could not start distributor")

        actual_port = port if self.args.proxy else (443 if route else distributor_node_ports[port])
        actual_ip = ip if self.args.proxy or self.args.in_cluster else (route if route else self.args.control_ip)

        print(f"- started {name} at {actual_ip}:{actual_port}")
        self.distributor = self.init_joinmarket_clientserver(
            name=name,
            port=actual_port,
            host=actual_ip,
            proxy=self.args.proxy
        )

        print(f"- started distributor")

    def prepare_additional_funding(self, wallets):
        """
        JoinMarket-specific additional funding setup.
        Creates and funds fidelity bonds for wallets that have bond configuration.
        """
        bond_clients = []
        bond_invoices = []

        print("Preparing fidelity bonds")

        for client, wallet in zip(self.clients, wallets):
            fidelity_bond_config = wallet.get("fidelity_bond", {})

            if not fidelity_bond_config.get("enabled", False):
                continue

            try:
                # Extract bond configuration
                amount = fidelity_bond_config.get("amount", 50000)  # Default 50k sats
                locktime = fidelity_bond_config.get("locktime")

                if not locktime:
                    print(f"Warning: No locktime specified for fidelity bond on {client.name}, skipping")
                    continue

                # Create the bond
                bond_info = client.create_fidelity_bond(
                    amount=amount,
                    locktime=locktime,
                    current_block=self.current_block
                )

                # Prepare funding invoice
                bond_address = bond_info['address']
                bond_invoice = (bond_address, amount)
                bond_invoices.append(bond_invoice)
                bond_clients.append((client, bond_address))

                print(f"- prepared fidelity bond for {client.name}: {amount} sats to {bond_address}")

            except Exception as e:
                print(f"Error creating fidelity bond for {client.name}: {e}")
                raise Exception(f"Failed to create fidelity bond for {client.name}: {e}")

        if bond_invoices:
            print(f"Funding {len(bond_invoices)} fidelity bonds")

            try:
                # Fund all bonds in a single batch
                self.pay_invoices(bond_invoices)

                # Mark bonds as funded
                for client, bond_address in bond_clients:
                    client.mark_bond_funded(bond_address)

                print(f"- funded {len(bond_invoices)} fidelity bonds")

                # Mine additional blocks to ensure fidelity bond transactions are confirmed
                # JoinMarket needs confirmed UTXOs to calculate bond values for maker offers
                print("Mining blocks to confirm fidelity bond transactions")
                for i in range(15):  # Mine 15 blocks for solid confirmation
                    self.node.mine_block()
                print("- fidelity bond confirmations completed")

            except Exception as e:
                print(f"Failed to fund fidelity bonds: {e}")
                raise Exception(f"Failed to fund fidelity bonds: {e}")
        else:
            print("- no fidelity bonds to fund")

    def start_orderbook_watch(self):
        name = "joinmarket-obwatch"
        port = 62601
        try:
            ip, obwatch_ports, route = self.driver.run(
                name,
                f"{self.args.image_prefix}joinmarket-client-server",
                env={"MODE": "obwatch"},
                ports={62601: port},
                cpu=0.25,
                memory=256,
                service_account="joinmarket",
                run_as_user=1000,
                run_as_group=1000,
                proxy=self.args.proxy
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            raise Exception("Could not start orderbook watcher")

        # Determine how to reach the service from the controller
        actual_port = 62601 if self.args.proxy else (443 if route else obwatch_ports[port])
        actual_ip = ip if self.args.proxy or self.args.in_cluster else (route if route else self.args.control_ip)

        print(f"- started {name} at {actual_ip}:{actual_port}")

        # Attach a lightweight client that periodically polls and stores snapshots under /tmp
        ob_client = OrderbookWatchClient(
            name=name,
            host=actual_ip,
            port=actual_port,
            type="orderbook",
        )
        self.obwatch_client = ob_client

    def store_engine_logs(self, data_path):
        # Store orderbook snapshots, grouped under data_path/orderbook/<client.name>
        print("- storing engine-logs")
        print(f"- storing {data_path}")
        ob_root = os.path.join(data_path, "orderbook")
        os.makedirs(ob_root, exist_ok=True)
        client = self.obwatch_client

        # Check if orderbook watcher client exists
        if client is None:
            print(f"- no orderbook watcher client to store")
            return

        src = getattr(client, "snapshot_dir", None)
        if not src or not os.path.isdir(src):
            print(f"- no snapshots to store for {client.name}")
            return
        dst = os.path.join(ob_root, client.name)
        os.makedirs(dst, exist_ok=True)
        try:
            # Prefer copytree with dirs_exist_ok when possible to preserve structure
            # Copy content of src into dst (merge)
            for root, dirs, files in os.walk(src):
                print(f"- found {root}")
                rel = os.path.relpath(root, src)
                target_dir = os.path.join(dst, rel) if rel != "." else dst
                os.makedirs(target_dir, exist_ok=True)
                for f in files:
                    print(f"- found {f}")
                    shutil.copy2(os.path.join(root, f), os.path.join(target_dir, f))
            print(f"- stored orderbook snapshots for {client.name}")
        except Exception as e:
            print(f"- could not store orderbook snapshots for {client.name}: {e}")


    @staticmethod
    def init_joinmarket_clientserver(name, port, host="localhost", proxy=None):
        print(f"Starting joinmarket-client-server: {name}")
        client = JoinMarketClientServer(name=name, port=port, host=host, proxy=proxy)

        ensure_client_session(client, name)

        if not client.wait_wallet(timeout=30000):
            print(f"- could not start {name} (application timeout)")
            raise Exception("Could not start distributor")
        return client


    def _reference_client_env(self) -> dict:
        return {"JM_MESSAGING": "directory" if self.directory_mode else "irc",
                "JM_DIRECTORY_NODES": NG_DIRECTORY_NODES}

    def _ng_client_env(self, wallet) -> dict:
        env = dict(NG_CLIENT_ENV)
        offers = wallet.get("offers") or []
        if wallet.get("type", "maker") == "maker" and offers:
            for offer_key, env_key in NG_OFFER_FACTOR_ENV.items():
                if offers[0].get(offer_key) is not None:
                    env[env_key] = f"{float(offers[0][offer_key]):.2f}"
        env.update(self.scenario.get("ng_env", {}))
        env.update(wallet.get("ng_env", {}))
        return {k: str(v) for k, v in env.items()}

    def start_client(self, idx: int, wallet=None):
        name = f"jcs-{idx:03}"
        port = 28184 + idx
        version = self.wallet_version(wallet or {})
        if version == NG_VERSION:
            # jmwalletd idles at ~80 MiB (python 3.14 + uvicorn); 128 fits a client with a
            # wallet and one active session, and keeps large runs inside namespace quotas
            image, env, cpu, memory = "joinmarket-ng", self._ng_client_env(wallet), 0.1, 128
        else:
            # A reference tumbler grows to ~91 MiB over a long schedule and gets OOMKilled at
            # the 64 Mi request (96 Mi limit); single-shot takers and makers stay well below it.
            memory = 128 if (wallet or {}).get("tumbler_options") else 64
            image, env, cpu = "joinmarket-client-server", self._reference_client_env(), 0.05
        try:
            print(f"Starting {image}: {name}")
            ip, client_node_ports, route = self.driver.run(
                name,
                f"{self.args.image_prefix}{image}",
                env=env,
                ports={28183: port},
                cpu=cpu,
                memory=memory,
                service_account="joinmarket",
                run_as_user=1000,
                run_as_group=1000,
                proxy=self.args.proxy
            )
            print(f"Started {image}: {name}")
        except Exception as e:
            print(f"- error starting {name}: {e}")
            return None

        # In kubernetes, the pod is addressed using the ip unique for that service and all pods have the port
        # 28183 in use. The port rotation is needed for the local docker run, where the ports are mapped to the local
        actual_port = 28183 if self.args.proxy else (443 if route else port)
        actual_ip = ip if self.args.proxy or self.args.in_cluster else (route if route else self.args.control_ip)

        print(f"- started {name} at {actual_ip}:{actual_port}")

        sleep(30)
        client = JoinMarketClientServer.from_wallet(
            name=name,
            port=actual_port,
            host=actual_ip,
            wallet=wallet,
            proxy=self.args.proxy,
            version=version)

        print(f"driver starting {name}")
        return client

    def stop_client(self, idx: int):
        name = f"jcs-{idx:03}"
        try:
            self.driver.stop(name)
        except Exception as e:
            print(f"- could not stop client {name}: {e}")

    def update_coinjoins_joinmarket(self):
        for client in self.clients:
            try:
                # Check if client just reached its limit
                was_active = not client.is_paused(self.current_block)
                delta = client.update(self.current_block, self.current_round)
                now_paused = client.is_paused(self.current_block)

                # Log when a client reaches its coinjoin limit
                if was_active and now_paused and hasattr(client, 'max_coinjoins') and client.max_coinjoins > 0:
                    if hasattr(client, 'completed_coinjoins') and client.completed_coinjoins >= client.max_coinjoins:
                        print(f"✓ {client.name} reached max coinjoins limit ({client.max_coinjoins})")

                # Apply any change in round count; alternatively, have the client trigger an event.
                self.current_round += delta
            except Exception as e:
                print(f"- could not update {client.name} ({e})")

        try:
            self.obwatch_client.update(self.current_block, self.current_round)
        except Exception as e:
            print(f"- could not update obwatch client ({e})")

    async def update_coinjoins_joinmarket_async(self):
        """
        Async version: Update all clients in parallel using asyncio.gather()
        Adds jitter between task creation to prevent synchronized RPC storms
        """
        # Create tasks for all client updates with jitter to desynchronize RPC calls
        client_tasks = []
        for client in self.clients:
            task = self._update_client_async(client)
            client_tasks.append(task)
            # Add jitter between task creations to desynchronize Bitcoin Core RPC calls
            jitter = random.uniform(0.01, 0.05)  # 10-50ms jitter
            await asyncio.sleep(jitter)

        # Add orderbook watcher client task if it exists
        if self.obwatch_client:
            obwatch_task = self._update_obwatch_async(self.obwatch_client)
            client_tasks.append(obwatch_task)

        # Run all updates concurrently
        results = await asyncio.gather(*client_tasks, return_exceptions=True)
        
        # Process results and update round count
        for i, result in enumerate(results[:-1] if self.obwatch_client else results):
            if isinstance(result, Exception):
                client_name = self.clients[i].name if i < len(self.clients) else "unknown"
                print(f"- could not update {client_name} ({result})")
            else:
                # Apply any change in round count
                self.current_round += result

    async def _update_client_async(self, client):
        """Helper to update a single client asynchronously"""
        try:
            delta = await client.update_async(self.current_block, self.current_round)
            return delta
        except Exception as e:
            print(f"- could not update {client.name} ({e})")
            return 0

    async def _update_obwatch_async(self, obwatch_client):
        """Helper to update orderbook watcher client asynchronously"""
        try:
            return await obwatch_client.update_async(self.current_block, self.current_round)
        except Exception as e:
            print(f"- could not update obwatch client ({e})")
            return 0

    async def cleanup_async_clients(self):
        """
        Cleanup async HTTP clients to prevent resource leaks.
        Should be called when shutting down the engine.
        """
        cleanup_tasks = []
        
        for client in self.clients:
            if hasattr(client, 'aclose'):
                cleanup_tasks.append(client.aclose())
        
        if self.obwatch_client and hasattr(self.obwatch_client, 'aclose'):
            cleanup_tasks.append(self.obwatch_client.aclose())
        
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            print("- closed all async HTTP clients")

    def check_client_resources(self):
        """
        Check resource usage for a sample of client pods.
        Logs memory usage and alerts if pods are near limits.
        """
        # Sample 5 random clients to avoid overhead
        import random
        sample_size = min(5, len(self.clients))
        sample_clients = random.sample(self.clients, sample_size) if self.clients else []

        high_usage_count = 0
        for client in sample_clients:
            stats = self.driver.get_pod_resource_usage(client.name)
            if stats:
                mem_mb = stats['memory_mb']
                mem_limit = stats['memory_limit_mb']
                mem_pct = stats['memory_percent']

                # Log if usage is over 80%
                if mem_pct > 80:
                    print(f"[RESOURCE WARNING] {client.name}: {mem_mb:.1f}/{mem_limit}MB ({mem_pct:.1f}%)")
                    high_usage_count += 1
                elif mem_pct > 60:
                    print(f"[RESOURCE] {client.name}: {mem_mb:.1f}/{mem_limit}MB ({mem_pct:.1f}%)")

        if high_usage_count > 0:
            print(f"[RESOURCE] {high_usage_count}/{sample_size} sampled pods using >80% memory")

    def shutdown_engine(self):
        """
        Shutdown the engine and cleanup resources.
        """
        if self.async_updates:
            try:
                asyncio.run(self.cleanup_async_clients())
            except Exception as e:
                print(f"- error during async client cleanup: {e}")

    def run_engine(self):
        # Note: Initial invoice payments now happen before this method is called
        try:
            initial_block = self.node.get_block_count()
        except Exception as e:
            print(f"- could not get initial block count: {e}")
            initial_block = 0
        for i in range(5):
            # Takers need 3 confirmations of transactions for the sourcing commitments
            self.node.mine_block()

        print(f"- coinjoin rounds: {self.current_round} (block {self.current_block})".ljust(60))

        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            while ( self.scenario["rounds"] == 0 or self.current_round < self.scenario["rounds"] ) and (
                    self.scenario["blocks"] == 0 or self.current_block < self.scenario["blocks"]):
                # refresh block count
                for _ in range(3):
                    try:
                        self.current_block = self.node.get_block_count() - initial_block
                        break
                    except Exception as e:
                        print(f"- could not get blocks".ljust(60), end="\r")
                        print(f"Block exception: {e}", file=sys.stderr)

                # Check resource usage every 5 minutes (10 iterations * 30s = 5 min)
                current_time = time()
                if current_time - self.last_resource_check > 300:  # 5 minutes
                    try:
                        self.check_client_resources()
                        self.last_resource_check = current_time
                    except Exception as e:
                        print(f"- resource check failed: {e}")

                # safe updates
                try:
                    self.update_invoice_payments()
                except Exception as e:
                    print(f"- invoice update failed: {e}")
                try:
                    if self.async_updates:
                        # Use async path for parallel client updates
                        self.loop.run_until_complete(self.update_coinjoins_joinmarket_async())
                    else:
                        # Use synchronous path (legacy)
                        self.update_coinjoins_joinmarket()
                except Exception as e:
                    print(f"- coinjoin update failed: {e}")
                print(
                    f"- coinjoin rounds: {self.current_round} (block {self.current_block})".ljust(60),
                    end="\r",
                )
                sleep(30)

            print()
            print(f"- limit reached")
            sleep(60)
            self.node.mine_block()

        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.close()


@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def ensure_client_session(client, name):
    if not client.session():
        print(f"- could not start {name} (session timeout)")
        raise Exception("Could not start distributor")