import time

from manager.btc_node import BtcNode
from manager import utils
from time import sleep
import random
import os
import json
import multiprocessing
import multiprocessing.pool
import math
import shutil
import datetime

DISTRIBUTOR_UTXOS = 200
BATCH_SIZE = 5  # Reduced from 5 to 1 to prevent UTXO race conditions
BTC = 100_000_000
LOG_DOWNLOAD_WORKERS = 8


class EngineBase:
    def __init__(self, args, driver, log_src_path):
        self.args = args
        self.driver = driver
        self.log_src_path = log_src_path
        self.scenario = self.default_scenario()
        self.versions = set()
        self.node: BtcNode = None
        self.distributor = None
        self.clients = []
        self.invoices = {}
        self.current_block = 0
        self.current_round = 0

    def default_scenario(self):
        raise NotImplementedError

    def load_scenario(self):
        if self.args.command == "run" and self.args.scenario:
            with open(self.args.scenario) as f:
                self.scenario.update(json.load(f))

        self.versions.add(self.scenario["default_version"])
        if "distributor_version" in self.scenario:
            self.versions.add(self.scenario["distributor_version"])
        for wallet in self.scenario["wallets"]:
            if "version" in wallet:
                self.versions.add(wallet["version"])


    def prepare_images(self):
        raise NotImplementedError

    def prepare_image(self, name: str, path=None):
        prefixed_name = self.args.image_prefix + name
        if self.driver.has_image(prefixed_name):
            if self.args.force_rebuild:
                if self.args.image_prefix:
                    self.driver.pull(prefixed_name)
                    print(f"- image pulled {prefixed_name}")
                else:
                    self.driver.build(name, f"./containers/{name}" if path is None else path)
                    print(f"- image rebuilt {prefixed_name}")
            else:
                print(f"- image reused {prefixed_name}")
        elif self.args.image_prefix:
            self.driver.pull(prefixed_name)
            print(f"- image pulled {prefixed_name}")
        else:
            self.driver.build(name, f"./containers/{name}" if path is None else path)
            print(f"- image built {prefixed_name}")

    def start_infrastructure(self):
        print("Starting infrastructure")
        self.start_btc_node()
        self.start_engine_infrastructure()
        self.start_distributor()

    def start_btc_node(self):
        btc_node_ip, btc_node_ports, _ = self.driver.run(
            "btc-node",
            f"{self.args.image_prefix}btc-node",
            ports={18443: 18443, 18444: 18444},
            cpu=2.0,
            memory=2048,
            service_account="btc-node",
        )

        print(btc_node_ip, btc_node_ports)
        self.node = BtcNode(
            host=btc_node_ip if self.args.proxy or self.args.in_cluster else self.args.control_ip,
            port=18443 if self.args.proxy else btc_node_ports[18443],
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
            if wallet.get("fidelity_bond", {}).get("enabled", False):
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
        fb_count = sum(1 for _, w in wallet_list if w.get("fidelity_bond", {}).get("enabled", False))
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
                    if client is None
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
            else:
                # After 3 retries, filter out None values
                new_clients = list(filter(lambda x: x is not None, new_clients))
                print(f"- failed to start {len(wallets) - len(new_clients)} clients; continuing ...")

        self.clients.extend(new_clients)


    def fund_distributor(self, btc_amount):
        print("Funding distributor")
        for _ in range(DISTRIBUTOR_UTXOS):
            self.node.fund_address(
                self.distributor.get_new_address(),
                math.ceil(btc_amount * BTC / DISTRIBUTOR_UTXOS) // BTC,
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

        # Client implementations may expose additional ground truth (e.g. history, tumbler plan)
        if hasattr(client, "export_ground_truth"):
            try:
                client.export_ground_truth(client_path)
            except Exception as e:
                print(f"- could not export ground truth for {client.name}: {e}")

        log_src_path = getattr(client, "log_src_path", self.log_src_path)
        try:
            self.driver.download(client.name, log_src_path, client_path)
            print(f"- stored {client.name} logs, {log_src_path}, {client_path}")
        except:
            print(f"- could not store {client.name} logs")

    def store_logs(self):
        print("Storing logs")
        time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        experiment_path = f"./logs/{time}_{self.scenario['name']}"
        data_path = os.path.join(experiment_path, "data")
        os.makedirs(data_path)

        with open(os.path.join(experiment_path, "scenario.json"), "w") as f:
            json.dump(self.scenario, f, indent=2)
            print("- stored scenario")

        stored_blocks = 0
        node_path = os.path.join(data_path, "btc-node")
        os.mkdir(node_path)
        try:
            while stored_blocks < self.node.get_block_count():
                block_hash = self.node.get_block_hash(stored_blocks)
                block = self.node.get_block_info(block_hash)
                with open(os.path.join(node_path, f"block_{stored_blocks}.json"), "w") as f:
                    json.dump(block, f, indent=2)
                stored_blocks += 1
        except TypeError:
            print("Failed to get block count")

        print(f"- stored {stored_blocks} blocks")

        print("- storing engine logs")
        print(f"{self.store_engine_logs}")
        self.store_engine_logs(data_path)
        print("- finished storing engine logs")

        print(f"- storing logs for {len(self.clients)} clients in parallel")
        # Bounded: each concurrent download holds a websocket and spools an archive, so an
        # unbounded pool (one worker per CPU) makes peak memory and API-server load scale with
        # the machine rather than with anything we control.
        with multiprocessing.pool.ThreadPool(LOG_DOWNLOAD_WORKERS) as pool:
            pool.starmap(self.store_client_logs, [(client, data_path) for client in self.clients])

        shutil.make_archive(experiment_path, "zip", *os.path.split(experiment_path))
        print("- zip archive created")

    def store_engine_logs(self, data_path):
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

    def update_invoice_payments(self):
        due = list(
            filter(
                lambda x: x[0] <= self.current_block and x[1] <= self.current_round, self.invoices.keys()
            )
        )
        for i in due:
            self.pay_invoices(self.invoices.pop(i, []))

    def prepare_invoices(self, wallets):
        print("Preparing invoices")
        client_invoices = [
            (client, wallet.get("funds", [])) for client, wallet in zip(self.clients, wallets)
        ]

        for client, funds in client_invoices:
            for fund in funds:
                block = 0
                round = 0
                if isinstance(fund, int):
                    value = fund
                elif isinstance(fund, dict):
                    value = fund.get("value", 0)
                    block = fund.get("delay_blocks", 0)
                    round = fund.get("delay_rounds", 0)
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
            for batch in utils.batched(addressed_invoices, BATCH_SIZE):
                for _ in range(3):
                    try:
                        result = self.distributor.send(batch)
                        if str(result) == "timeout":
                            print("- transaction timeout")
                            continue
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
        print(f"=== Scenario {self.scenario['name']} ===")
        self.prepare_images()
        self.start_infrastructure()
        self.fund_distributor(5000)
        self.start_clients(self.scenario["wallets"])
        time.sleep(60)
        self.prepare_invoices(self.scenario["wallets"])

        # Pay initial wallet funding invoices before additional funding
        print("Paying initial wallet funding")
        self.update_invoice_payments()

        # Allow engines to perform additional post-funding setup (e.g., fidelity bonds)
        self.prepare_additional_funding(self.scenario["wallets"])

        print("Running simulation")
        self.run_engine()


    def run_engine(self):
        raise NotImplementedError