import asyncio
import json
import os
import random
import shutil
import sys
import threading
from collections.abc import Iterator
from time import sleep, time
from typing import cast

import backoff

from manager.engine.base.manifest import ProducerLabelEvidence
from manager.engine.configuration import JoinMarketConfig, JoinMarketRole, ScenarioConfig, WalletConfig
from manager.engine.engine_base import EngineBase
from manager.engine.joinmarket.events import (
    collect_round_events,
    match_round_events_to_blocks,
    producer_label_evidence,
)
from manager.engine.joinmarket.round_confirmation import (
    confirm_rounds_in_blocks,
    count_confirmed_rounds,
    mark_latest_started_round_failed,
)
from manager.engine.joinmarket.round_event_record import RoundEvent
from manager.wasabi_clients.joinmarket_clients.joinmarket_client_base import JoinMarketClientServer
from manager.wasabi_clients.joinmarket_clients.joinmarket_clients import OrderbookWatchClient


class JoinmarketEngine(EngineBase):

    def __init__(self, args, driver):
        super().__init__(args, driver,
                         log_src_path="/home/joinmarket/.joinmarket/logs")
        self.obwatch_client = None
        self._obwatch_missing_logged = False
        # Feature flag to enable async client updates (default: enabled for better performance)
        self.async_updates = getattr(args, 'async_updates', True)
        self.loop = None
        self.last_resource_check = 0  # Track when we last checked resources
        self._core_wallet_lock = threading.Lock()
        self._round_scan_height = -1

    def default_scenario(self) -> ScenarioConfig:
        return ScenarioConfig(
            name="default",
            default_version="joinmarket",
            rounds=0,  # the number of coinjoins after which the simulation stops (0 for no limit)
            blocks=0,  # the number of mined blocks after which the simulation stops (0 for no limit)
            wallets=[
                WalletConfig(
                    funds=[75000, 75000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.TAKER,
                        tumbler_options={'addrcount': 3, 'minmakercount': 4, 'makercountrange': [5, 1], 'mixdepthcount': 3, 'mintxcount': 2, 'txcountparams': [3, 1], 'timelambda': 5, 'stage1_timelambda_increase': 1, 'liquiditywait': 60, 'waittime': 20, 'mixdepthsrc': 0, 'restart': True, 'mincjamount': 35000, 'amtmixdepths': 4, 'rounding_chance': 0, 'rounding_sigfig_weights': [55, 15, 25, 65, 45]},
                    ),
                ),
                WalletConfig(
                    funds=[1000000, 500000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[3000000, 15000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[1000000, 500000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[3000000, 600000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[200000, 50000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[3000000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[1000000, 500000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[3000000, 15000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[1000000, 500000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
                WalletConfig(
                    funds=[3000000, 600000],
                    joinmarket=JoinMarketConfig(
                        role=JoinMarketRole.MAKER,
                        offers=[{'txfee': 0, 'cjfee_a': 5000, 'cjfee_r': 4e-05, 'ordertype': 'sw0reloffer', 'minsize': 30000, 'maxsize': 3000000}],
                    ),
                ),
            ],
        )

    def prepare_images(self):
        print("Preparing images")
        self.prepare_image("btc-node")
        base_image = self.prepare_joinmarket_base_image()
        self.prepare_image(
            "joinmarket-client-server",
            build_args={"JOINMARKET_BASE_IMAGE": base_image} if base_image else None,
        )
        self.prepare_image("irc-server")

    def prepare_joinmarket_base_image(self):
        """Build the vendored JoinMarket base for a local client build.

        Return its tag for JOINMARKET_BASE_IMAGE, or None to use the default base.
        """
        if not self.local_build_requested("joinmarket-client-server"):
            return None
        base_path = "./vendor/joinmarket-clientserver"
        if not os.path.isdir(base_path):
            print(f"- vendored JoinMarket source missing at {base_path}; using the published base")
            return None
        base_image = f"{self.args.image_prefix}joinmarket-base:latest"
        self.driver.build(base_image, base_path)
        print(f"- image built {base_image}")
        return base_image


    @staticmethod
    def core_wallet_name(client_name):
        """Bitcoin Core wallet owned by a single JoinMarket container."""
        return f"jm_wallet_{client_name}"

    def joinmarket_container_env(self, rpc_wallet_file):
        return {"JM_RPC_WALLET_FILE": rpc_wallet_file}

    def validate_clients(self):
        super().validate_clients()
        roles = {getattr(client, "type", "") for client in self.clients}
        if "taker" not in roles:
            raise RuntimeError("JoinMarket scenario requires at least one started taker client")
        if "maker" not in roles:
            raise RuntimeError("JoinMarket scenario requires at least one started maker client")

    def create_core_wallet(self, client_name):
        """Create the container's own Bitcoin Core wallet and return its name."""
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        wallet_name = self.core_wallet_name(client_name)
        # Clients are started in parallel, and Core rejects concurrent
        # createwallet calls on the same node.
        with self._core_wallet_lock:
            self.node.create_wallet(wallet_name, disable_private_keys=True)
        print(f"- created {wallet_name} in BitcoinCore")
        return wallet_name

    def start_engine_infrastructure(self):
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")

        self.start_irc_server()
        print("- started irc-server")

        # Start the JoinMarket orderbook watcher service and attach a client to poll it
        try:
            self.start_orderbook_watch()
            print("- started orderbook watcher")
        except Exception as e:
            print(f"- could not start orderbook watcher ({e})")


    def start_irc_server(self):
        # TODO: When the container fails to start, the exception is not thrown and it is not recognized.
        name = "irc-server"

        try:
            ip, manager_ports, _ = self.driver.run(
                name,
                self.image_ref("irc-server"),
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
        core_wallet = self.create_core_wallet(name)
        try:
            ip, distributor_node_ports, route = self.driver.run(
                name,
                self.image_ref("joinmarket-client-server"),
                env=self.joinmarket_container_env(core_wallet),
                ports={28183: port},
                cpu=1,
                memory=1024,
                service_account="joinmarket"
            )
        except Exception as e:
            print(f"- could not start {name} ({e})")
            raise Exception("Could not start distributor")

        actual_ip, actual_port = self.service_endpoint(ip, port, distributor_node_ports, route, tls=True)

        print(f"- started {name} at {actual_ip}:{actual_port}")
        self.distributor = self.init_joinmarket_clientserver(
            name=name,
            port=actual_port,
            host=actual_ip,
            proxy=self.args.proxy
        )

        print("- started distributor")

    def prepare_additional_funding(self, wallets):
        """
        JoinMarket-specific additional funding setup.
        Creates and funds fidelity bonds for wallets that have bond configuration.
        """
        bond_clients = []
        bond_invoices = []

        print("Preparing fidelity bonds")

        for client, wallet in zip(self.clients, wallets):
            fidelity_bond_config = (wallet.joinmarket.fidelity_bond if wallet.joinmarket else None) or {}

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
                if self.node is None:
                    raise RuntimeError("Bitcoin node is not initialized")
                for _ in range(15):  # Mine 15 blocks for solid confirmation
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
                self.image_ref("joinmarket-client-server"),
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
        actual_ip, actual_port = self.service_endpoint(ip, 62601, obwatch_ports, route)

        print(f"- started {name} at {actual_ip}:{actual_port}")

        # Attach a lightweight client that periodically polls and stores snapshots under /tmp
        ob_client = OrderbookWatchClient(
            name=name,
            host=actual_ip,
            port=actual_port,
            type="orderbook",
        )
        self.obwatch_client = ob_client

    def collect_round_events(self) -> list[RoundEvent]:
        """Copy producer-owned round records from all clients."""
        return collect_round_events([
            cast(list[RoundEvent], getattr(client, "round_events", []))
            for client in self.clients
        ])

    def live_round_events(self) -> list[RoundEvent]:
        return [
            event
            for client in self.clients
            for event in getattr(client, "round_events", [])
        ]

    def confirm_started_rounds(self) -> int:
        """Count a round only once its unique destination appears in a mined block."""
        if self.node is None:
            return 0
        events = self.live_round_events()
        self._round_scan_height = confirm_rounds_in_blocks(events, self.node, self._round_scan_height)
        self.current_round = count_confirmed_rounds(events)
        return self.current_round

    def _round_limit_reached(self) -> bool:
        """True once the confirmed rounds satisfy the scenario, so no taker may start another attempt."""
        return self.scenario.rounds > 0 and self.current_round >= self.scenario.rounds

    def _mark_taker_round_failed(self, taker_name: str, reason: str) -> None:
        mark_latest_started_round_failed(self.live_round_events(), taker_name, reason, self.current_block)

    def match_joinmarket_rounds_to_blocks(self, data_path: str) -> list[RoundEvent]:
        """Reconcile copied client records with blocks exported under data_path."""
        node_path = os.path.join(data_path, "btc-node")

        def exported_blocks() -> Iterator[dict[str, object]]:
            """Yield exported blocks one at a time to avoid retaining them all."""
            if not os.path.isdir(node_path):
                return
            for filename in sorted(os.listdir(node_path)):
                if filename.startswith("block_") and filename.endswith(".json"):
                    with open(os.path.join(node_path, filename), encoding="utf-8") as stream:
                        yield cast(dict[str, object], json.load(stream))

        return match_round_events_to_blocks(self.collect_round_events(), exported_blocks())

    def store_round_events(self, data_path: str) -> ProducerLabelEvidence:
        """Store reconciled labels and return their producer-label evidence."""
        labels = self.match_joinmarket_rounds_to_blocks(data_path)
        with open(os.path.join(data_path, "joinmarket_round_events.json"), "w", encoding="utf-8") as stream:
            json.dump(labels, stream, indent=2)
        print(f"- stored {len(labels)} JoinMarket round labels")
        return dict(producer_label_evidence(labels, self._unlabelled_takers()))

    def _unlabelled_takers(self) -> list[str]:
        """Tumblers that cannot emit a producer-owned record per round."""
        return [
            str(getattr(client, "name", "?"))
            for client in self.clients
            if getattr(client, "tumbler_options", None) and not getattr(client, "round_events", None)
        ]

    def store_engine_logs(self, data_path):
        print("- storing engine-logs")
        self.store_orderbook_snapshots(data_path)
        return self.store_round_events(data_path)

    def store_orderbook_snapshots(self, data_path):
        # Store orderbook snapshots, grouped under data_path/orderbook/<client.name>
        print(f"- storing {data_path}")
        ob_root = os.path.join(data_path, "orderbook")
        os.makedirs(ob_root, exist_ok=True)
        client = self.obwatch_client

        # Check if orderbook watcher client exists
        if client is None:
            print("- no orderbook watcher client to store")
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

        if not client.wait_wallet(timeout=120):
            print(f"- could not start {name} (application timeout)")
            raise Exception("Could not start distributor")
        return client


    def start_client(self, idx: int, wallet: WalletConfig | None = None):
        name = f"jcs-{idx:03}"
        port = 28184 + idx
        core_wallet = self.create_core_wallet(name)
        try:
            print(f"Starting joinmarket-client-server: {name}")
            ip, client_node_ports, route = self.driver.run(
                name,
                self.image_ref("joinmarket-client-server"),
                env=self.joinmarket_container_env(core_wallet),
                ports={28183: port},
                cpu=(0.05),
                memory=(64),
                service_account="joinmarket",
                run_as_user=1000,
                run_as_group=1000,
                proxy=self.args.proxy
            )
            print(f"Started joinmarket-client-server: {name}")
        except Exception as e:
            print(f"- error starting {name}: {e}")
            return None

        # In kubernetes, the pod is addressed using the ip unique for that service and all pods have the port
        # 28183 in use. The port rotation is needed for the local docker run, where the ports are mapped to the local
        actual_ip, actual_port = self.service_endpoint(
            ip, 28183, client_node_ports or {28183: port}, route, tls=True
        )

        print(f"- started {name} at {actual_ip}:{actual_port}")

        sleep(30)
        client = JoinMarketClientServer.from_wallet(
            name=name,
            port=actual_port,
            host=actual_ip,
            wallet=wallet,
            proxy=self.args.proxy)

        print(f"driver starting {name}")
        return client

    def stop_client(self, idx: int):
        name = f"jcs-{idx:03}"
        try:
            self.driver.stop(name)
        except Exception as e:
            print(f"- could not stop client {name}: {e}")

    def _note_missing_obwatch(self):
        """Report once that the orderbook watcher is unavailable for this run.

        Startup failures are tolerated, so a missing watcher is a valid state.
        Reporting it every round would drown the log, so this is said once.
        """
        if self._obwatch_missing_logged:
            return
        self._obwatch_missing_logged = True
        print("- orderbook watcher is not running, skipping its updates for this run")

    def update_coinjoins_joinmarket(self):
        self.confirm_started_rounds()
        if self._round_limit_reached():
            return
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

                # An RPC start is only an attempt; the round counter is owned by
                # confirm_started_rounds(). A negative delta is a timed-out attempt.
                if delta < 0:
                    self._mark_taker_round_failed(client.name, "coinjoin attempt timed out")
            except Exception as e:
                print(f"- could not update {client.name} ({e})")

        if self.obwatch_client is not None:
            try:
                self.obwatch_client.update(self.current_block, self.current_round)
            except Exception as e:
                print(f"- could not update obwatch client ({e})")
        else:
            self._note_missing_obwatch()

    async def update_coinjoins_joinmarket_async(self):
        """
        Async version: Update all clients in parallel using asyncio.gather()
        Adds jitter between task creation to prevent synchronized RPC storms
        """
        self.confirm_started_rounds()
        if self._round_limit_reached():
            return
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
        else:
            self._note_missing_obwatch()

        # Run all updates concurrently
        results = await asyncio.gather(*client_tasks, return_exceptions=True)
        
        # Process results and update round count
        for i, result in enumerate(results[:-1] if self.obwatch_client else results):
            if isinstance(result, Exception):
                client_name = self.clients[i].name if i < len(self.clients) else "unknown"
                print(f"- could not update {client_name} ({result})")
            elif isinstance(result, int) and result < 0:
                self._mark_taker_round_failed(self.clients[i].name, "coinjoin attempt timed out")

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

    WARMUP_BLOCKS = 5

    def start_block_clock(self) -> int:
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")
        for _ in range(self.WARMUP_BLOCKS):
            self.node.mine_block()
        try:
            return self.node.get_block_count()
        except Exception as e:
            print(f"- could not get initial block count: {e}")
            return 0

    def run_engine(self):
        if self.node is None:
            raise RuntimeError("Bitcoin node is not initialized")

        # Note: Initial invoice payments now happen before this method is called
        initial_block = self.start_block_clock()

        print(f"- coinjoin rounds: {self.current_round} (block {self.current_block})".ljust(60))

        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            while (self.scenario.rounds == 0 or self.current_round < self.scenario.rounds ) and (
                    self.scenario.blocks == 0 or self.current_block < self.scenario.blocks):
                # refresh block count
                for _ in range(3):
                    try:
                        self.current_block = self.node.get_block_count() - initial_block
                        break
                    except Exception as e:
                        print("- could not get blocks".ljust(60), end="\r")
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
            print("- limit reached")
            sleep(60)
            # Mine confirmations for the last broadcast before artifacts are captured.
            self.node.mine_block(3)

        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.close()


@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def ensure_client_session(client, name):
    if not client.probe_session():
        print(f"- could not start {name} (session timeout)")
        raise Exception("Could not start distributor")
