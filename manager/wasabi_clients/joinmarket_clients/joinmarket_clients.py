import uuid
import os
import json
import time
from datetime import datetime
import requests
import httpx

from .joinmarket_client_base import JoinMarketClientServer

class MakerClient(JoinMarketClientServer):
    """
    This class implements the logic for a maker.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.maker_running = False

    def update_status(self):
        """
        Get the status of the client and update the maker_running flag.
        """
        response = super().update_status()
        self.maker_running = response.get("maker_running", False)
        return response

    def update(self, current_block, current_round) -> int:
        """
        Start the maker if it is not running.
        """
        self.update_status()

        if self.is_paused(current_block):
            return 0

        if not self.maker_running:
            offer = self.get_offer(current_round)

            # Log fidelity bond status for debugging
            if hasattr(self, 'fidelity_bonds') and self.fidelity_bonds:
                bond_count = len(self.fidelity_bonds)
                funded_bonds = sum(1 for bond in self.fidelity_bonds.values() if bond.get('funded', False))
                print(f"Starting maker {self.name} with {funded_bonds}/{bond_count} funded fidelity bonds")
            else:
                print(f"Starting maker {self.name} (no fidelity bonds)")

            self.start_maker(**offer)
            self.maker_running = True
        return 0

    async def update_async(self, current_block, current_round) -> int:
        """
        Async version: Start the maker if it is not running.
        """
        response = await self.update_status_async()
        self.maker_running = response.get("maker_running", False)

        if self.is_paused(current_block):
            return 0

        if not self.maker_running:
            offer = self.get_offer(current_round)

            # Log fidelity bond status for debugging
            if hasattr(self, 'fidelity_bonds') and self.fidelity_bonds:
                bond_count = len(self.fidelity_bonds)
                funded_bonds = sum(1 for bond in self.fidelity_bonds.values() if bond.get('funded', False))
                print(f"Starting maker {self.name} with {funded_bonds}/{bond_count} funded fidelity bonds")
            else:
                print(f"Starting maker {self.name} (no fidelity bonds)")

            await self.start_maker_async(**offer)
            self.maker_running = True
        return 0

class TakerClient(JoinMarketClientServer):
    """
    This class implements the logic for a taker that does *not* have tumbler options.
    """

    def update_status(self):
        """
        Get the status of the client and update the coinjoin_in_process flag.
        """
        response = super().update_status()
        was_in_process = self.coinjoin_in_process
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)

        # Detect coinjoin completion and increment counter
        if was_in_process and not self.coinjoin_in_process:
            self.completed_coinjoins += 1
            limit_str = f"/{self.max_coinjoins}" if self.max_coinjoins > 0 else ""
            print(f"Coinjoin completed for {self.name} (completed {self.completed_coinjoins}{limit_str})")

        return response

    def update(self, current_block, current_round):
        """
        Start a coinjoin if none is running and the client is not paused.
        Stop the coinjoin if it has been running for 8 blocks.
        """
        self.update_status()

        delta = 0
        if not self.coinjoin_in_process and not self.is_paused(current_block):
            offer = self.get_offer(current_round)
            offer["destination"] = self.get_new_address()
            self.start_coinjoin(**offer)
            self.coinjoin_start = current_block
            self.coinjoin_in_process = True
            delta = +1
            print(f"Starting coinjoin {self.name}")
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))

        # TODO: The 8 block limit could be a parameter.
        elif self.coinjoin_in_process and self.coinjoin_start + 8 < current_block:
            self.stop_coinjoin()
            self.coinjoin_in_process = False
            self.next_coinjoin_allowed = current_block + self.time_between_rounds
            delta = -1
            print(f"Stopping coinjoin {self.name} (timeout after 8 blocks)")
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))
        return delta

    async def update_async(self, current_block, current_round):
        """
        Async version: Start a coinjoin if none is running and the client is not paused.
        Stop the coinjoin if it has been running for 8 blocks.
        """
        # Update status (which will increment completed_coinjoins if coinjoin finished)
        response = await self.update_status_async()
        was_in_process = self.coinjoin_in_process
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)

        # Detect coinjoin completion and increment counter
        if was_in_process and not self.coinjoin_in_process:
            self.completed_coinjoins += 1
            limit_str = f"/{self.max_coinjoins}" if self.max_coinjoins > 0 else ""
            print(f"Coinjoin completed for {self.name} (completed {self.completed_coinjoins}{limit_str})")

        # Early return if paused
        if self.is_paused(current_block):
            return 0

        delta = 0
        if not self.coinjoin_in_process:
            offer = self.get_offer(current_round)
            offer["destination"] = self.get_new_address()
            await self.start_coinjoin_async(**offer)
            self.coinjoin_start = current_block
            self.coinjoin_in_process = True
            delta = +1
            print(f"Starting coinjoin {self.name}")
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))

        # TODO: The 8 block limit could be a parameter.
        elif self.coinjoin_in_process and self.coinjoin_start + 8 < current_block:
            self.stop_coinjoin()
            self.coinjoin_in_process = False
            self.next_coinjoin_allowed = current_block + self.time_between_rounds
            delta = -1
            print(f"Stopping coinjoin {self.name} (timeout after 8 blocks)")
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))
        return delta


class OrderbookWatchClient(JoinMarketClientServer):
    """
    A lightweight client that periodically queries the JoinMarket ob-watcher HTTP endpoint
    and stores snapshots to disk under /tmp to avoid large memory usage.
    """
    def __init__(self, **kwargs):
        # The joinmarket-ng watcher serves only /orderbook.json (no refresh / bonds endpoints)
        self.supports_refresh = kwargs.pop("supports_refresh", True)
        self.supports_bonds = kwargs.pop("supports_bonds", True)
        # Default port for ob-watcher is 62601 and it is plain HTTP (not HTTPS)
        super().__init__(**kwargs)
        self.ob_host = kwargs.get("host", self.host)
        self.ob_port = int(kwargs.get("port", 62601))
        self.snapshot_dir = kwargs.get("snapshot_dir", f"/tmp/jm-orderbook/{self.name}")
        # Default minimum polling interval is 1 minute
        self.poll_interval_sec = int(kwargs.get("poll_interval_sec", 60))
        self._last_poll_ts = 0

        os.makedirs(self.snapshot_dir, exist_ok=True)

    def _fetch_orderbook(self):
        url = f"http://{self.ob_host}:{self.ob_port}/orderbook.json"
        resp = requests.get(
            url,
            timeout=10,
            proxies=dict(http=self.proxy) if self.proxy else None
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_fidelity_bonds(self):
        """Fetch fidelity bonds data for debugging"""
        if not self.supports_bonds:
            return None
        url = f"http://{self.ob_host}:{self.ob_port}/fidelitybonds"
        try:
            resp = requests.get(
                url,
                timeout=10,
                proxies=dict(http=self.proxy) if self.proxy else None
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[FidelityBonds] Failed to fetch bonds data: {e}")
            return None

    def _store_snapshot(self, data: dict):
        # Group by date directory and name files orderbook_HH-MM.json
        date_dir = datetime.now().strftime("%Y-%m-%d")
        time_part = datetime.now().strftime("%H-%M")
        target_dir = os.path.join(self.snapshot_dir, date_dir)
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, f"orderbook_{time_part}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

    def update(self, current_block, current_round) -> int:
        """
        Periodically poll the orderbook and store a snapshot to disk.
        Also periodically check fidelity bonds endpoint for debugging.
        Returns 0 to avoid altering round counts.
        """
        now = time.time()
        if now - self._last_poll_ts < self.poll_interval_sec:
            print(f"Skipping {self.name} since last poll")
            return 0

        try:
            # Fetch and store orderbook data
            data = self._fetch_orderbook()
            path = self._store_snapshot(data)
            print(f"[Orderbook] Stored snapshot for {self.name} at {path}")

            # Debug: Check fidelity bonds endpoint and log the data
            bonds_data = self._fetch_fidelity_bonds()
            if bonds_data is not None:
                # Log summary of fidelity bonds found
                if isinstance(bonds_data, list):
                    print(f"[FidelityBonds] Found {len(bonds_data)} fidelity bonds in database")
                    # Log details of first few bonds for debugging
                    for i, bond in enumerate(bonds_data[:3]):  # Show first 3 bonds
                        if isinstance(bond, dict):
                            bond_value = bond.get('value', 0)
                            utxo = bond.get('utxo', 'unknown')
                            print(f"[FidelityBonds] Bond {i+1}: value={bond_value}, utxo={utxo}")
                elif isinstance(bonds_data, dict):
                    print(f"[FidelityBonds] Bonds data: {bonds_data}")
                else:
                    print(f"[FidelityBonds] Unexpected bonds data format: {type(bonds_data)}")
            else:
                print("[FidelityBonds] No bonds data retrieved from endpoint")

        except Exception as e:
            print(f"[Orderbook] Failed to fetch/store snapshot for {self.name}: {e}")
        finally:
            self._last_poll_ts = now

        return 0

    async def _refresh_orderbook_async(self):
        """Refresh the orderbook by calling the refreshorderbook endpoint."""
        if not self.supports_refresh:
            return True
        url = f"http://{self.ob_host}:{self.ob_port}/refreshorderbook"
        proxy_config = self.proxy if self.proxy else None

        try:
            async with httpx.AsyncClient(proxy=proxy_config, timeout=10.0) as client:
                response = await client.post(url)
                response.raise_for_status()
                return True
        except Exception as e:
            print(f"[Orderbook] Error refreshing orderbook: {e}")
            return False

    async def _fetch_orderbook_async(self):
        """Async version of _fetch_orderbook using httpx"""
        # First refresh the orderbook
        await self._refresh_orderbook_async()
        
        # Then fetch the updated orderbook
        url = f"http://{self.ob_host}:{self.ob_port}/orderbook.json"
        proxy_config = self.proxy if self.proxy else None

        try:
            async with httpx.AsyncClient(proxy=proxy_config, timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"[Orderbook] Error fetching orderbook: {e}")
            return None

    async def _fetch_fidelity_bonds_async(self):
        """Async version of _fetch_fidelity_bonds using httpx"""
        if not self.supports_bonds:
            return None
        url = f"http://{self.ob_host}:{self.ob_port}/fidelitybonds"
        proxy_config = self.proxy if self.proxy else None

        try:
            async with httpx.AsyncClient(proxy=proxy_config, timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            print(f"[FidelityBonds] Failed to fetch bonds data: {e}")
            return None

    async def update_async(self, current_block, current_round) -> int:
        """
        Async version: Periodically poll the orderbook and store a snapshot to disk.
        Also periodically check fidelity bonds endpoint for debugging.
        Returns 0 to avoid altering round counts.
        """
        now = time.time()
        if now - self._last_poll_ts < self.poll_interval_sec:
            print(f"Skipping {self.name} since last poll")
            return 0

        try:
            # Fetch and store orderbook data
            data = await self._fetch_orderbook_async()
            path = self._store_snapshot(data)
            print(f"[Orderbook] Stored snapshot for {self.name} at {path}")

            # Debug: Check fidelity bonds endpoint and log the data
            bonds_data = await self._fetch_fidelity_bonds_async()
            if bonds_data is not None:
                # Log summary of fidelity bonds found
                if isinstance(bonds_data, list):
                    print(f"[FidelityBonds] Found {len(bonds_data)} fidelity bonds in database")
                    # Log details of first few bonds for debugging
                    for i, bond in enumerate(bonds_data[:3]):  # Show first 3 bonds
                        if isinstance(bond, dict):
                            bond_value = bond.get('value', 0)
                            utxo = bond.get('utxo', 'unknown')
                            print(f"[FidelityBonds] Bond {i+1}: value={bond_value}, utxo={utxo}")
                elif isinstance(bonds_data, dict):
                    print(f"[FidelityBonds] Bonds data: {bonds_data}")
                else:
                    print(f"[FidelityBonds] Unexpected bonds data format: {type(bonds_data)}")
            else:
                print("[FidelityBonds] No bonds data retrieved from endpoint")

        except Exception as e:
            print(f"[Orderbook] Failed to fetch/store snapshot for {self.name}: {e}")
        finally:
            self._last_poll_ts = now

        return 0

class TumblerTakerClient(JoinMarketClientServer):
    """
    This subclass is for a taker with tumbler options.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tumbler_options = kwargs.get("tumbler_options", None)
        if self.tumbler_options:
            self.tumbler_options['schedulefile'] = self.tumbler_options['schedulefile'] + "_" + uuid.uuid4().hex
        self.last_schedule = None
        self.coinjoin_completed = True

    def update_status(self):
        """
        Get the status of the client and updates flags
        The conjoin_completed flag is determined by comparing the current and
        last schedule, which gets updated after each coinjoin.
        """
        response = super().update_status()
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)
        schedule = response.get("schedule", None)
        if schedule != self.last_schedule:
            self.coinjoin_completed = True
            self.last_schedule = schedule
        return response

    def update(self, current_block, current_round):
        """
        Start a coinjoin if none is running and the client is not paused.
        Increment the round count if a coinjoin has completed.
        """
        self.update_status()

        if not self.coinjoin_in_process and not self.is_paused(current_block):
            print(f"Starting scheduled coinjoin for {self.name}")
            response = self.run_schedule()
            self.last_schedule = response["schedule"]
            print(response)
            self.coinjoin_in_process = True
            self.coinjoin_start = current_block
            return 0

        delta = 0
        if self.coinjoin_completed:
            delta = +1
            self.coinjoin_completed = False
            print("Coinjoin for {self.name} completed.")
            print(self.get_schedule())
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))

        return delta

    async def update_async(self, current_block, current_round):
        """
        Async version: Start a coinjoin if none is running and the client is not paused.
        Increment the round count if a coinjoin has completed.
        """
        response = await self.update_status_async()
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)
        schedule = response.get("schedule", None)
        if schedule != self.last_schedule:
            self.coinjoin_completed = True
            self.last_schedule = schedule

        if not self.coinjoin_in_process and not self.is_paused(current_block):
            print(f"Starting scheduled coinjoin for {self.name}")
            response = await self.run_schedule_async()
            self.last_schedule = response["schedule"]
            print(response)
            self.coinjoin_in_process = True
            self.coinjoin_start = current_block
            return 0

        delta = 0
        if self.coinjoin_completed:
            delta = +1
            self.coinjoin_completed = False
            print("Coinjoin for {self.name} completed.")
            print(await self.get_schedule_async())
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))

        return delta