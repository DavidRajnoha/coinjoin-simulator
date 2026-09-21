import asyncio
import json
from time import sleep, time
from typing import List

import httpx
import requests
import urllib3
from bip_utils import Bip32Slip10Secp256k1, Bip39SeedGenerator

from manager.engine.joinmarket.round_event_record import (
    EXECUTION_STATUS_REQUESTED,
    EXECUTION_STATUS_STARTED,
    EXECUTION_STATUS_UNKNOWN,
)
from manager.exceptions import RpcError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


WALLET_NAME = "wallet"
DEFAULT_WAIT_WALLET_TIMEOUT = 60
DEFAULT_COINJOIN_TIMEOUT_BLOCKS = 8
PASSWORD = "password"
WALLET_TYPE = "sw"
BTC = 100_000_000


class JoinmarketConflictException(Exception):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class JoinmarketServiceStateError(RpcError):
    """jmwalletd answered 401 about the service state, not about the credentials.

    jmwalletd maps ServiceNotStarted ("Service cannot be stopped as it is not
    running."), ServiceAlreadyStarted and WalletAlreadyUnlocked to HTTP 401 as
    well; only its InvalidToken answers carry a ``WWW-Authenticate`` header and
    only InvalidCredentials says "Invalid credentials.". Unlocking the wallet
    and retrying does nothing for the state answers.
    """


def _is_auth_failure(status_code, headers, body_text) -> bool:
    if status_code != 401:
        return False
    if "WWW-Authenticate" in headers:
        return True
    try:
        message = json.loads(body_text or "").get("message", "")
    except (ValueError, AttributeError):
        return True
    return message == "Invalid credentials."


def _error_message(response) -> str:
    try:
        return response.json().get("message") or response.text
    except ValueError:
        return response.text


class JoinMarketClientServer:
    def __init__(
        self,
        host="localhost",
        port=28183,
        walletname=WALLET_NAME,
        name="joinmarket-client-server",
        proxy="",
        version="",
        type="maker",
        delay=(0, 0),
        stop=(0, 0),
        offers=None,
        tumbler_options=None,
        time_between_rounds=0,
        coinjoin_timeout_blocks=DEFAULT_COINJOIN_TIMEOUT_BLOCKS,
        has_fidelity_bonds=False,
        max_coinjoins=0,
    ):
        self.host = host
        self.port = port
        self.walletname = walletname  # Store walletname as an instance variable
        self.name = name
        self.proxy = proxy
        self.version = version
        self.type = type
        self.maker_running = False
        self.coinjoin_in_process = False
        self.coinjoin_start = 0
        self.next_coinjoin_allowed = delay[0]
        self.time_between_rounds = time_between_rounds
        if coinjoin_timeout_blocks <= 0:
            raise ValueError("coinjoin_timeout_blocks must be positive")
        self.coinjoin_timeout_blocks = coinjoin_timeout_blocks
        self.stop = stop
        self.token = ""
        self.refresh_token = ""
        self.offers = offers if offers else []
        self.tumbler_options = tumbler_options if tumbler_options else None
        self.coin_history = {}
        self.seedphrase = ""
        self.has_fidelity_bonds = has_fidelity_bonds
        self.max_coinjoins = max_coinjoins
        self.completed_coinjoins = 0

        # Fidelity bond tracking
        self.fidelity_bonds = {}  # Track created bonds: {address: {amount, locktime, creation_block}}
        # Producer-owned ground truth: one record per coinjoin this client starts.
        self.round_events: list[dict[str, object]] = []

        # Async HTTP client setup
        self._async_client = None
        self._unlock_lock = None  # Will be created when needed
        self._client_initialized = False

    def _ensure_async_client(self):
        """Initialize the async HTTP client if not already done."""
        if self._async_client is None or self._async_client.is_closed:
            # Configure proxy for httpx (correct syntax)
            proxy_config = self.proxy if self.proxy else None
            
            self._async_client = httpx.AsyncClient(
                base_url=f"https://{self.host}:{self.port}/api/v1",
                verify=False,
                proxy=proxy_config,  # httpx uses 'proxy', not 'proxies'
                timeout=httpx.Timeout(60.0),
                http2=True
            )
            self._client_initialized = True
        return self._async_client

    async def aclose(self):
        """Close the async HTTP client."""
        if self._async_client and not self._async_client.is_closed:
            await self._async_client.aclose()
            self._async_client = None
            self._client_initialized = False

    @classmethod
    def offers_for_wallet(cls, joinmarket, role):
        """Configured offers, or the legacy defaults for a role that declares none."""
        configured = [dict(offer) for offer in (joinmarket.offers if joinmarket else None) or []]
        return configured or cls._default_offers(role)

    @staticmethod
    def _default_offers(role):
        """Offers a scenario that only declares a role used to get implicitly."""
        # Defaults removed from JoinmarketEngine in d2ec87aba092e8acc775768c7d0b9bad84357492.
        if role == "maker":
            return [{
                "txfee": 0,
                "cjfee_a": 5000,
                "cjfee_r": 0.00004,
                "ordertype": "sw0reloffer",
                "minsize": 30000,
                "maxsize": 3000000,
            }]
        if role == "taker":
            return [{"mixdepth": 0, "amount_sats": 40000, "counterparties": 4}]
        return []

    @classmethod
    def from_wallet(cls, name: str, port: int, wallet, host: str, proxy=""):
        joinmarket = getattr(wallet, "joinmarket", None)
        type_ = joinmarket.role.value if joinmarket and joinmarket.role else "maker"
        tumbler_options = (joinmarket.tumbler_options if joinmarket else None) or {}

        # Check if wallet has fidelity bonds configured
        fidelity_bond = (joinmarket.fidelity_bond if joinmarket else None) or {}
        has_fidelity_bonds = bool(fidelity_bond.get("enabled", False))

        # Select the appropriate subclass based on wallet config.
        client_cls: type["JoinMarketClientServer"]
        if type_ == "maker":
            from manager.wasabi_clients.joinmarket_clients.joinmarket_clients import MakerClient
            client_cls = MakerClient
        elif type_ == "taker":
            # Distinguish between a standard taker and a tumbler taker based on tumbler_options.
            if tumbler_options:
                from manager.wasabi_clients.joinmarket_clients.joinmarket_clients import TumblerTakerClient
                client_cls = TumblerTakerClient
            else:
                from manager.wasabi_clients.joinmarket_clients.joinmarket_clients import TakerClient
                client_cls = TakerClient
        else:
            client_cls = cls

        # Instantiate the client using the selected subclass.
        client = client_cls(
            name=name,
            port=port,
            type=type_,
            delay=(wallet.delay_blocks or 0, wallet.delay_rounds or 0),
            stop=(wallet.stop_blocks or 0, wallet.stop_rounds or 0),
            offers=cls.offers_for_wallet(joinmarket, type_),
            tumbler_options=tumbler_options,
            time_between_rounds=(joinmarket.time_between_rounds if joinmarket else 0) or 0,
            coinjoin_timeout_blocks=(
                joinmarket.coinjoin_timeout_blocks
                if joinmarket and joinmarket.coinjoin_timeout_blocks is not None
                else DEFAULT_COINJOIN_TIMEOUT_BLOCKS
            ),
            has_fidelity_bonds=has_fidelity_bonds,
            max_coinjoins=(joinmarket.max_coinjoins if joinmarket else None) or 0,
            host=host,
            proxy=proxy
        )

        start = time()
        if not client.wait_wallet(timeout=120):
            print(
                f"- could not start {name} (application timeout {time() - start} seconds)"
            )
            return None

        print(f"- started {client.name} (wait took {time() - start} seconds)")
        return client

    def coinjoin_timed_out(self, current_block: int) -> bool:
        """Whether the active CoinJoin attempt exceeded its configured block limit."""
        return self.coinjoin_start + self.coinjoin_timeout_blocks < current_block

    def update_status(self) -> dict:
        self.update_coin_history()
        return self.session()

    def _rpc(self, method, endpoint, json_data=None, timeout=60, repeat=4, log_errors=True) -> dict:
        url = f"https://{self.host}:{self.port}/api/v1{endpoint}"
        headers = {}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        response = None
        for attempt in range(repeat):
            try:
                print(f"[RPC] {method} {url} (attempt {attempt+1}/{repeat}) data={json_data} using proxy={self.proxy}")
                response = requests.request(
                    method=method,
                    url=url,
                    json=json_data or {},
                    headers=headers,
                    proxies=dict(https=self.proxy) if self.proxy else None,
                    timeout=timeout,
                    verify=False,
                )
                print(f"[RPC] Response {response.status_code}: {response.text}")

                if response.status_code == 401:
                    if not _is_auth_failure(401, response.headers, response.text):
                        raise JoinmarketServiceStateError(_error_message(response))
                    print("[RPC] 401 Unauthorized: Attempting to unlock wallet and retry...")
                    self.unlock_wallet()
                    headers['Authorization'] = f'Bearer {self.token}'
                    continue

                if response.status_code == 409:
                    print(f"[RPC] 409 Conflict: {response.text}")
                    raise JoinmarketConflictException(f"Error {response.status_code}: {response.text}", response)

                if response.status_code >= 400:
                    try:
                        print(response.json())
                        error_message = response.json().get("message", "Unknown error")
                    except json.JSONDecodeError:
                        error_message = response.text
                    print(f"[RPC] Error {response.status_code}: {error_message}")
                    raise Exception(f"Error {response.status_code}: {error_message}")

                return response.json()
            except JoinmarketServiceStateError:
                raise
            except Exception as e:
                if log_errors:
                    print(f"[RPC ERROR] {method} {url}: {e}")
                if attempt == repeat - 1:
                    raise
                sleep(1)
        raise RpcError(f"{method} {endpoint} stayed unauthorized after {repeat} attempts")

    async def _rpc_async(self, method, endpoint, json_data=None, timeout=60, repeat=4) -> dict:
        """Async version of _rpc using httpx.AsyncClient."""
        client = self._ensure_async_client()
        headers = {}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        
        for attempt in range(repeat):
            try:
                print(f"[RPC-ASYNC] {method} {endpoint} (attempt {attempt+1}/{repeat}) data={json_data} using proxy={self.proxy}")
                
                response = await client.request(
                    method=method,
                    url=endpoint,
                    json=json_data or {},
                    headers=headers,
                    timeout=timeout
                )
                print(f"[RPC-ASYNC] Response {response.status_code}: {response.text}")

                if response.status_code == 401:
                    if not _is_auth_failure(401, response.headers, response.text):
                        raise JoinmarketServiceStateError(_error_message(response))
                    print("[RPC-ASYNC] 401 Unauthorized: Attempting to unlock wallet and retry...")
                    await self.unlock_wallet_async()
                    headers['Authorization'] = f'Bearer {self.token}'
                    continue

                if response.status_code == 409:
                    print(f"[RPC-ASYNC] 409 Conflict: {response.text}")
                    raise JoinmarketConflictException(f"Error {response.status_code}: {response.text}", response)

                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        error_message = error_data.get("message", "Unknown error")
                    except Exception:
                        error_message = response.text
                    print(f"[RPC-ASYNC] Error {response.status_code}: {error_message}")
                    response.raise_for_status()

                return response.json()
            except JoinmarketServiceStateError:
                raise
            except httpx.HTTPStatusError as e:
                print(f"[RPC-ASYNC ERROR] {method} {endpoint}: HTTP {e.response.status_code}")
                if attempt == repeat - 1:
                    raise Exception(f"HTTP Error {e.response.status_code}: {e.response.text}")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[RPC-ASYNC ERROR] {method} {endpoint}: {e}")
                if attempt == repeat - 1:
                    raise
                await asyncio.sleep(1)

        raise RpcError(f"{method} {endpoint} stayed unauthorized after {repeat} attempts")

    def is_paused(self, current_block):
        # Check delay - "delay[0]" means "don't run until current_block >= delay[0]"
        if current_block < self.next_coinjoin_allowed:
            return True
        # Check max coinjoins limit
        if self.max_coinjoins > 0 and self.completed_coinjoins >= self.max_coinjoins:
            return True
        return False



    def session(self):
        try:
            method = "GET"
            endpoint = "/session"
            response = self._rpc(method, endpoint)
            return response
        except Exception as e:
            print(e)
            return None

    def probe_session(self):
        """One ``/session`` request for the startup readiness wait.

        A daemon that is not listening yet is the expected answer here, so the
        connection error is reported as progress, not as ``[RPC ERROR]``.
        """
        try:
            return self._rpc("GET", "/session", repeat=1, log_errors=False)
        except Exception as e:
            print(f"- jmwalletd on {self.host}:{self.port} not ready yet ({type(e).__name__})")
            return None

    def _create_wallet(self, walletname=None, wallettype=None):
        """Create a new wallet and store its name."""
        method = "POST"
        endpoint = "/wallet/create"
        walletname = walletname or self.walletname
        wallet_type = wallettype or WALLET_TYPE
        data = {
            "walletname": walletname,
            "password": PASSWORD,
            "wallettype": wallet_type
        }
        # Use a longer timeout for wallet creation (slow clients)
        response = self._rpc(method, endpoint, json_data=data, timeout=300)
        self.token = response.get("token", "")
        self.refresh_token = response.get("refresh_token", "")
        self.seedphrase = response.get("seedphrase", "")
        return response

    def unlock_wallet(self, password=None):
        """Unlock an existing wallet using the stored walletname."""
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/unlock"
        json_data = {"password": password or PASSWORD}
        response = self._rpc(method, endpoint, json_data=json_data)
        self.token = response.get("token", "")
        self.refresh_token = response.get("refresh_token", "")
        return response

    async def update_status_async(self) -> dict:
        """Async version of update_status"""
        await self.update_coin_history_async()
        session_response = await self.session_async()
        return session_response if session_response is not None else {}

    async def session_async(self):
        """Async version of session"""
        try:
            method = "GET"
            endpoint = "/session"
            response = await self._rpc_async(method, endpoint)
            return response if response is not None else {}
        except Exception as e:
            print(f"Session async error: {e}")
            return {}

    async def run_schedule_async(self):
        """Async version of run_schedule"""
        if not self.tumbler_options:
            raise Exception("No tumbler options provided")
        address_count = self.tumbler_options.get("address_count", 3)
        destination_addresses = [self.get_new_address() for _ in range(address_count)]

        method = "POST"
        endpoint = f"/wallet/{self.walletname}/taker/schedule"
        json_data = {
            "destination_addresses": destination_addresses,
            "tumbler_options": self.tumbler_options
        }

        start = time()
        while time() - start < 60:  # Using a longer timeout for the more complex tumbler operation
            try:
                response = await self._rpc_async(method, endpoint, json_data=json_data)
                return response
            except Exception:
                if time() - start >= 60:
                    print("Failed to run schedule, attempt timed out.")
                await asyncio.sleep(1)  # Add a small delay between retries

        raise TimeoutError(f"Could not run the tumbler schedule for {self.walletname}")

    async def get_schedule_async(self):
        """Async version of get_schedule"""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/taker/schedule"
        response = await self._rpc_async(method, endpoint)
        return response

    async def update_coin_history_async(self):
        """
        Async version: Poll the current unspent coins (UTXOs) from the API and update the internal
        coin history. Expects the API to return a dict with a key "utxos" that is a list
        of coin dicts.
        """
        try:
            response = await self.list_utxos_async()
            if response is None:
                print("Error: list_utxos_async returned None")
                return
            # Extract the list of coins from the JSON structure.
            coins = response.get("utxos", [])
        except Exception as e:
            print(f"Error fetching UTXOs: {e}")
            return

        for coin in coins:
            key = coin.get("utxo")
            if key:
                # setdefault ensures we record a coin only once
                self.coin_history.setdefault(key, coin)

    async def unlock_wallet_async(self, password=None):
        """Async unlock of an existing wallet using the stored walletname."""
        # Lazy creation of async lock when needed
        if self._unlock_lock is None:
            self._unlock_lock = asyncio.Lock()
        async with self._unlock_lock:
            method = "POST"
            endpoint = f"/wallet/{self.walletname}/unlock"
            json_data = {"password": password or PASSWORD}
            response = await self._rpc_async(method, endpoint, json_data=json_data)
            self.token = response.get("token", "")
            self.refresh_token = response.get("refresh_token", "")
            return response

    def _retry_until_deadline(self, step, deadline):
        """Retry step with exponential backoff until the deadline passes."""
        delay = 1.0
        while True:
            try:
                step()
                return
            except Exception:
                remaining = deadline - time()
                if remaining <= 0:
                    raise
                sleep(min(delay, remaining))
                delay = min(delay * 2, 8.0)

    def _wait_wallet_create(self, deadline):
        def create():
            elapsed = int(time() - self._wait_wallet_start)
            wallet_type = "sw-fb" if self.has_fidelity_bonds else WALLET_TYPE
            print(f"- trying wallet creation for {self.walletname} on {self.host}:{self.port} (elapsed {elapsed}s, type: {wallet_type})")
            self._create_wallet(wallettype=wallet_type)

        self._retry_until_deadline(create, deadline)

    def _wait_wallet_display(self, deadline):
        def display():
            elapsed = int(time() - self._wait_wallet_start)
            print(f"- checking wallet display for {self.walletname} on {self.host}:{self.port} (elapsed {elapsed}s)")
            self.get_balance()
            print(f"- wallet {self.walletname} ready on {self.host}:{self.port}")

        self._retry_until_deadline(display, deadline)

    def wait_wallet(self, timeout=None):
        """
        Wait for the wallet to become available, using separate exponential backoff for creation and display.

        The timeout is the budget for each of the two phases; it used to be
        ignored in favour of a fixed 60 seconds.
        """
        self._wait_wallet_start = time()
        budget = DEFAULT_WAIT_WALLET_TIMEOUT if timeout is None else timeout
        try:
            try:
                self._wait_wallet_create(time() + budget)
            except Exception as e:
                print(f"- wallet {self.walletname} creation failed: {e}")
                raise
            self._wait_wallet_display(time() + budget)
            return True
        except Exception:
            print(f"[TIMEOUT] Wallet {self.walletname} not ready after {int(time() - self._wait_wallet_start)}s on {self.host}:{self.port}")
            return False

    def display_wallet(self, display_all=False):
        """Get detailed breakdown of wallet contents by account."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/display"
        if display_all:
            endpoint += "?displayall=true"
        response = self._rpc(method, endpoint)
        return response

    async def display_wallet_async(self, display_all=False):
        """Async get detailed breakdown of wallet contents by account."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/display"
        if display_all:
            endpoint += "?displayall=true"
        response = await self._rpc_async(method, endpoint)
        return response

    def get_balance(self):
        """Retrieve the available balance of the wallet.
        Returns: str: The available balance as a string in BTC (e.g., '0.00000000').
        Raises: Exception: If the balance information cannot be retrieved.
        """
        response = self.display_wallet()
        try:
            available_balance = response['walletinfo']['available_balance']
            return int(float(available_balance) * BTC)
        except KeyError as e:
            raise Exception(f"Could not retrieve available balance: {e}")

    async def get_balance_async(self):
        """Async retrieve the available balance of the wallet.
        Returns: str: The available balance as a string in BTC (e.g., '0.00000000').
        Raises: Exception: If the balance information cannot be retrieved.
        """
        response = await self.display_wallet_async()
        try:
            available_balance = response['walletinfo']['available_balance']
            return int(float(available_balance) * BTC)
        except KeyError as e:
            raise Exception(f"Could not retrieve available balance: {e}")

    def get_yieldgen_report(self):
        """Get the latest report on yield-generating activity."""
        method = "GET"
        endpoint = "/wallet/yieldgen/report"
        response = self._rpc(method, endpoint)
        return response

    def get_new_address(self, mixdepth=0):
        """Get a fresh address in the given account for depositing funds."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/address/new/{mixdepth}"
        response = self._rpc(method, endpoint)
        return response['address']

    def get_new_timelock_address(self, lockdate):
        """Get a fresh timelock address for depositing funds to create a fidelity bond."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/address/timelock/new/{lockdate}"
        response = self._rpc(method, endpoint)
        return response

    def create_fidelity_bond(self, amount, locktime, current_block=0):
        """
        Create a fidelity bond by generating a timelock address and tracking it.

        Args:
            amount: Amount in satoshis to bond
            locktime: Unix timestamp when bond unlocks
            current_block: Current block height (for tracking)

        Returns:
            dict: Bond information including address
        """
        try:
            response = self.get_new_timelock_address(locktime)
            address = response.get('address')

            if not address:
                raise Exception(f"Failed to create fidelity bond address: {response}")

            # Track the bond
            self.fidelity_bonds[address] = {
                'amount': amount,
                'locktime': locktime,
                'creation_block': current_block,
                'funded': False
            }

            print(f"Created fidelity bond address {address} for {amount} sats until {locktime}")
            return {
                'address': address,
                'amount': amount,
                'locktime': locktime,
                'creation_block': current_block
            }

        except Exception as e:
            raise Exception(f"Failed to create fidelity bond: {e}")

    def get_fidelity_bonds(self):
        """
        Get list of all created fidelity bonds.

        Returns:
            dict: Dictionary of bond addresses to bond info
        """
        return self.fidelity_bonds.copy()

    def mark_bond_funded(self, address):
        """
        Mark a fidelity bond as funded.

        Args:
            address: Bond address that was funded
        """
        if address in self.fidelity_bonds:
            self.fidelity_bonds[address]['funded'] = True
            print(f"Marked fidelity bond {address} as funded")
        else:
            print(f"Warning: Attempted to mark unknown bond address {address} as funded")

    def get_bond_value(self, address, current_block=0):
        """
        Calculate bond value for reputation (simplified calculation).

        Args:
            address: Bond address
            current_block: Current block height

        Returns:
            float: Bond value for reputation calculation
        """
        if address not in self.fidelity_bonds:
            return 0.0

        bond = self.fidelity_bonds[address]
        if not bond['funded']:
            return 0.0

        # Simplified bond value calculation
        # In real JoinMarket, this involves complex age/amount calculations
        amount_btc = bond['amount'] / BTC
        blocks_held = max(0, current_block - bond['creation_block'])

        # Basic age-weighted value (simplified)
        age_factor = min(1.0, blocks_held / 144)  # Blocks per day
        return amount_btc * age_factor

    def export_fidelity_bonds_data(self, current_block=0):
        """
        Export fidelity bond data for logging/analysis.

        Args:
            current_block: Current block height for value calculations

        Returns:
            dict: Complete fidelity bond information with calculated values
        """
        bonds_data = {
            "client_name": self.name,
            "wallet_name": self.walletname,
            "wallet_type": "sw-fb" if self.has_fidelity_bonds else "sw",
            "current_block": current_block,
            "bonds": []
        }

        for address, bond_info in self.fidelity_bonds.items():
            bond_data = {
                "address": address,
                "amount_satoshis": bond_info["amount"],
                "amount_btc": bond_info["amount"] / BTC,
                "locktime": bond_info["locktime"],
                "creation_block": bond_info["creation_block"],
                "funded": bond_info["funded"],
                "bond_value": self.get_bond_value(address, current_block),
                "blocks_held": max(0, current_block - bond_info["creation_block"]) if bond_info["funded"] else 0
            }
            bonds_data["bonds"].append(bond_data)

        bonds_data["total_bonds"] = len(bonds_data["bonds"])
        bonds_data["total_amount_satoshis"] = sum(b["amount_satoshis"] for b in bonds_data["bonds"])
        bonds_data["total_amount_btc"] = bonds_data["total_amount_satoshis"] / BTC
        bonds_data["total_bond_value"] = sum(b["bond_value"] for b in bonds_data["bonds"])

        return bonds_data

    def list_utxos(self):
        """List details of all UTXOs currently in the wallet."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/utxos"
        response = self._rpc(method, endpoint)
        return response

    async def list_utxos_async(self):
        """Async list details of all UTXOs currently in the wallet."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/utxos"
        response = await self._rpc_async(method, endpoint)
        return response

    def start_maker(
        self,
        txfee,
        cjfee_a,
        cjfee_r,
        ordertype,
        minsize,
        maxsize
    ):
        """
        Start the yield generator service with the specified configuration.
        - txfee: str or int, e.g., "0" (absolute fee in satoshis)
        - cjfee_a: str or int, e.g., "5000" (absolute coinjoin fee in satoshis)
        - cjfee_r: str or float, e.g., "0.00004" (relative coinjoin fee as a fraction)
        - ordertype: str, e.g., "reloffer" or "absoffer"
        - minsize: str or int, minimum coinjoin size in satoshis. Should be higher then 27300sats
        """
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/maker/start"
        json_data = {
            "txfee": str(txfee),
            "cjfee_a": str(cjfee_a),
            "cjfee_r": str(cjfee_r),
            "ordertype": ordertype,
            "minsize": str(minsize),
            "maxsize": str(maxsize)
        }

        try:
            response = self._rpc(method, endpoint, json_data=json_data)
        except JoinmarketConflictException as e:
            print("Could not start maker without confirmed balance")
            response = e.response

        return response

    async def start_maker_async(
        self,
        txfee,
        cjfee_a,
        cjfee_r,
        ordertype,
        minsize,
        maxsize
    ):
        """
        Async start the yield generator service with the specified configuration.
        - txfee: str or int, e.g., "0" (absolute fee in satoshis)
        - cjfee_a: str or int, e.g., "5000" (absolute coinjoin fee in satoshis)
        - cjfee_r: str or float, e.g., "0.00004" (relative coinjoin fee as a fraction)
        - ordertype: str, e.g., "reloffer" or "absoffer"
        - minsize: str or int, minimum coinjoin size in satoshis. Should be higher then 27300sats
        """
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/maker/start"
        json_data = {
            "txfee": str(txfee),
            "cjfee_a": str(cjfee_a),
            "cjfee_r": str(cjfee_r),
            "ordertype": ordertype,
            "minsize": str(minsize),
            "maxsize": str(maxsize)
        }

        try:
            response = await self._rpc_async(method, endpoint, json_data=json_data)
        except JoinmarketConflictException as e:
            print("Could not start maker without confirmed balance")
            response = e.response

        return response

    def stop_maker(self):
        """Stop the yield generator service."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/maker/stop"
        try:
            return self._rpc(method, endpoint)
        except JoinmarketServiceStateError as e:
            # jmwalletd answers 401 ServiceNotStarted for an idle maker; nothing to stop.
            print(f"- {self.name}: maker already stopped ({e})")
            return {}

    async def stop_maker_async(self):
        """Async stop the yield generator service."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/maker/stop"
        try:
            return await self._rpc_async(method, endpoint)
        except JoinmarketServiceStateError as e:
            print(f"- {self.name}: maker already stopped ({e})")
            return {}

    def record_round_start(
        self,
        destination: str,
        amount_sats: int | None,
        counterparties: int | None,
        mixdepth: int | None,
        current_block: int,
        chain_height: int | None = None,
    ) -> dict[str, object]:
        """Record an attempt before its start RPC so its destination can be reconciled with blocks."""
        event = {
            "round_id": len(self.round_events) + 1,
            "engine": "joinmarket",
            "status": "started",
            "execution_status": EXECUTION_STATUS_REQUESTED,
            "taker": self.name,
            "destination_address": destination,
            "amount_sats": amount_sats,
            "counterparties": counterparties,
            "mixdepth": mixdepth,
            "start_block": current_block,
            "start_chain_height": chain_height,
        }
        self.round_events.append(event)
        return event

    def record_round_start_outcome(self, event: dict[str, object], acknowledged: bool) -> None:
        """Record whether jmwalletd acknowledged the start; otherwise the chain decides the outcome."""
        if acknowledged:
            event["execution_status"] = EXECUTION_STATUS_STARTED
            return
        event["execution_status"] = EXECUTION_STATUS_UNKNOWN
        print(f"- round {event['round_id']} of {self.name} got no answer to its start; the chain decides its outcome")

    def start_coinjoin(
        self,
        mixdepth,
        amount_sats,
        counterparties,
        destination,
        txfee=None
    ):
        """
        Initiate a coinjoin as taker.
        - mixdepth: int, the mixdepth to spend from
        - amount_sats: int, amount in satoshis to coinjoin
        - counterparties: int, number of counterparties to coinjoin with
        - destination: str, address to send the coinjoined funds to
        - txfee: optional, int, Bitcoin miner fee to use for transaction
        """
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/taker/coinjoin"
        json_data = {
            "mixdepth": mixdepth,
            "amount_sats": amount_sats,
            "counterparties": counterparties,
            "destination": destination
        }
        if txfee is not None:
            json_data["txfee"] = txfee
        response = self._rpc(method, endpoint, json_data=json_data)
        return response

    async def start_coinjoin_async(
        self,
        mixdepth,
        amount_sats,
        counterparties,
        destination,
        txfee=None
    ):
        """
        Async initiate a coinjoin as taker.
        - mixdepth: int, the mixdepth to spend from
        - amount_sats: int, amount in satoshis to coinjoin
        - counterparties: int, number of counterparties to coinjoin with
        - destination: str, address to send the coinjoined funds to
        - txfee: optional, int, Bitcoin miner fee to use for transaction
        """
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/taker/coinjoin"
        json_data = {
            "mixdepth": mixdepth,
            "amount_sats": amount_sats,
            "counterparties": counterparties,
            "destination": destination
        }
        if txfee is not None:
            json_data["txfee"] = txfee
        response = await self._rpc_async(method, endpoint, json_data=json_data)
        return response

    def run_schedule(self):
        """
        Create and run a schedule of transactions.
        - destination_addresses: list of str, addresses to send funds to
        - tumbler_options: optional, dict, additional tumbler configuration options
        """
        if not self.tumbler_options:
            raise Exception("No tumbler options provided")
        address_count = self.tumbler_options.get("address_count", 3)
        destination_addresses = [self.get_new_address() for _ in range(address_count)]

        method = "POST"
        endpoint = f"/wallet/{self.walletname}/taker/schedule"
        json_data = {
            "destination_addresses": destination_addresses,
            "tumbler_options": self.tumbler_options
        }

        start = time()
        while time() - start < 60:  # Using a longer timeout for the more complex tumbler operation
            try:
                response = self._rpc(method, endpoint, json_data=json_data)
                return response
            except Exception:
                if time() - start >= 60:
                    print("Failed to run schedule, attempt timed out.")
                sleep(1)  # Add a small delay between retries

        raise TimeoutError(f"Could not run the tumbler schedule for {self.walletname}")

    def get_schedule(self):
        """Get the schedule that is currently running."""
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/taker/schedule"
        response = self._rpc(method, endpoint)
        return response

    def stop_coinjoin(self):
        """Stop a running coinjoin attempt."""
        try:
            if self.type == "taker" and self.coinjoin_in_process:
                return self.stop_taker()
            elif self.type == "maker" and self.maker_running:
                return self.stop_maker()
            else:
                print("No coinjoin in process")
                return True
        except Exception as e:
            print(f"Failed to stop coinjoin: {e}")
            return False

    def stop_taker(self):
        method = "GET"
        endpoint = f"/wallet/{self.walletname}/taker/stop"
        try:
            return self._rpc(method, endpoint)
        except JoinmarketServiceStateError as e:
            # jmwalletd answers 401 ServiceNotStarted for an idle taker; nothing to stop.
            print(f"- {self.name}: taker already stopped ({e})")
            return {}

    def send(self, addressed_fundings):
        try:
            for address, amount in addressed_fundings:
                self.simple_send(destination_address=address, amount_sats=amount)
                print(f"- sent {amount} sats to {address}")
                sleep(5)  # The btc node needs time to process the transaction
        except Exception as e:
            print(f"- error during fund distribution: {e}")
            raise e


    def simple_send(self, destination_address, amount_sats, mixdepth=0, txfee=5000):
        """
        Send funds to a single address without coinjoin.
        - destination_address: str, address to send funds to
        - amount_sats: int, amount in satoshis to send
        - mixdepth: int, the mixdepth to spend from
        - txfee: int, miner fee in satoshis
        """
        method = "POST"
        endpoint = f"/wallet/{self.walletname}/taker/direct-send"
        json_data = {
            "destination": destination_address,
            "amount_sats": amount_sats,
            "txfee": txfee,
            "mixdepth": mixdepth,
        }
        start = time()
        while time() - start < 30:
            try:
                response = self._rpc(method, endpoint, json_data=json_data)
                return response
            except Exception as e:
                print(e)
                sleep(2)

        print("Failed to send funds, attempt timed out.")

        return False

    def list_transactions_maker(self):
        """List all transactions in the wallet."""
        method = "GET"
        endpoint = "/wallet/yieldgen/report"
        response = self._rpc(method, endpoint)
        return response

    def list_unspent_coins(self):
        """List all unspent coins in the wallet."""
        try:
            response = self.list_utxos()
            coins = response.get("utxos", [])
        except Exception as e:
            print("Error fetching UTXOs:", e)
            return
        return self.transform_coins_to_wasabi(coins)


    def list_coins(self):
        """List all coins in the wallet."""
        return self.transform_coins_to_wasabi(
            list(self.coin_history.values()))

    def list_keys(self):
        """List all addresses reported by JoinMarket's complete wallet display."""
        seed_bytes = Bip39SeedGenerator(self.seedphrase).Generate()
        walletinfo = self.display_wallet(display_all=True).get("walletinfo") or {}
        keys = []
        for account in walletinfo.get("accounts") or []:
            for branch in account.get("branches") or []:
                for entry in branch.get("entries") or []:
                    if not entry.get("address"):
                        continue
                    key_path = str(entry.get("hd_path") or "")
                    key = {
                        "full_key_path": key_path,
                        "pubKey": None,
                        "internal": None,
                        "address": str(entry["address"]),
                        "path": key_path,
                        "account": str(account.get("account", "")),
                        "status": str(entry.get("status", "")),
                        "amount": str(entry.get("amount", "")),
                    }
                    if key_path and ":" not in key_path:
                        try:
                            bip32_ctx = Bip32Slip10Secp256k1.FromSeedAndPath(seed_bytes, key_path)
                            key["pubKey"] = bip32_ctx.PublicKey().RawUncompressed().ToHex()
                            key["internal"] = key_path.split("/")[-2] == "1"
                        except Exception as error:
                            print(f"Error processing key path '{key_path}': {error}")
                    keys.append(key)
        return keys

    def get_offer(self, round=0):
        return self.offers[round % len(self.offers)]


    def update_coin_history(self):
        """
        Poll the current unspent coins (UTXOs) from the API and update the internal
        coin history. Expects the API to return a dict with a key "utxos" that is a list
        of coin dicts.
        """
        try:
            response = self.list_utxos()
            # Extract the list of coins from the JSON structure.
            coins = response.get("utxos", [])
        except Exception as e:
            print("Error fetching UTXOs:", e)
            return

        for coin in coins:
            key = coin.get("utxo")
            if key:
                # setdefault ensures we record a coin only once
                self.coin_history.setdefault(key, coin)


    def transform_coins_to_wasabi(self, joinmarket_coins: List):
        """
        Transform joinmarket's UTXO output to the Wasabi Wallet format.

        Expected joinmarket coin format (example):
        {
            "address": "bcrt1q0ldnp64tctudcfjuy89radpzecplauy7dvdf78",
            "path": "m/84'/1'/0'/0/0",
            "label": "",
            "value": 200000,
            "tries": 0,
            "tries_remaining": 3,
            "external": False,
            "mixdepth": 0,
            "confirmations": 1,
            "frozen": False,
            "utxo": "2585ed322655adfc4645f28196b77ad7d56ab1d389eb00a044719d42995ad4c5:1"
        }

        Desired Wasabi format (example):
        {
            "txid": "2585ed322655adfc4645f28196b77ad7d56ab1d389eb00a044719d42995ad4c5",
            "index": 1,
            "amount": 200000,
            "anonymityScore": 1,
            "confirmed": true,
            "confirmations": 1,
            "keyPath": "84'/1'/0'/0/0",
            "address": "bcrt1q0ldnp64tctudcfjuy89radpzecplauy7dvdf78"
        }
        """
        wasabi_coins = []
        for coin in joinmarket_coins:
            utxo_str = coin.get("utxo", "")
            if not utxo_str:
                continue
            try:
                txid, index_str = utxo_str.split(":")
                index = int(index_str)
            except Exception as e:
                print(f"Could not parse utxo '{utxo_str}': {e}")
                continue

            key_path = coin.get("path", "")
            if key_path.startswith("m/"):
                key_path = key_path[2:]

            wasabi_coin = {
                "txid": txid,
                "index": index,
                "amount": coin.get("value"),
                "confirmed": coin.get("confirmations", 0) > 0,
                "confirmations": coin.get("confirmations", 0),
                "keyPath": key_path,
                "address": coin.get("address"),
            }
            if "spentBy" in coin:
                wasabi_coin["spentBy"] = coin["spentBy"]

            wasabi_coins.append(wasabi_coin)
        return wasabi_coins
