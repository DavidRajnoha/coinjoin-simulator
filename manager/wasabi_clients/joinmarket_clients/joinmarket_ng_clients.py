"""
Clients for joinmarket-ng (jmwalletd). The HTTP API is JAM-compatible, so the
reference client classes are reused; only the deviations are overridden here:

- maker/start takes no ``maxsize`` and no privacy-enhanced factors (those are
  container settings ``MAKER__*_FACTOR``),
- the tumbler is driven through ``/tumbler/plan|start|status|stop`` with a
  persistent plan instead of ``/taker/schedule``,
- ground truth is exported from ``/history``, ``/tumbler/status`` and ``/logs``.
"""
import json
import os

import requests

from .joinmarket_client_base import JoinMarketClientServer, JoinmarketConflictException
from .joinmarket_clients import MakerClient, TakerClient

NG_DATA_DIR = "/home/joinmarket/.joinmarket-ng"

# tumbler_options keys consumed by the emulator, not forwarded to TumbleParameters
TUMBLER_LOCAL_OPTIONS = {
    "address_count", "addrcount", "restart", "schedulefile", "max_replans", "max_plans",
    "max_plan_start_failures",
}
TERMINAL_PLAN_STATES = {"completed", "failed", "cancelled"}


def _norm(value) -> str:
    # NG enums may serialize as "completed" or "PhaseStatus.COMPLETED"
    return str(value).split(".")[-1].lower()


class JoinMarketNGClient(JoinMarketClientServer):
    log_src_path = NG_DATA_DIR

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_session = {}
        self.last_plan = None

    # ------------------------------------------------------------------ status

    def session(self):
        response = super().session()
        if response:
            self.last_session = response
        return response

    async def session_async(self):
        response = await super().session_async()
        if response:
            self.last_session = response
        return response

    # ------------------------------------------------------------------ maker

    @staticmethod
    def _maker_payload(txfee, cjfee_a, cjfee_r, ordertype, minsize, **_ignored):
        return {
            "txfee": str(txfee),
            "cjfee_a": str(cjfee_a),
            "cjfee_r": str(cjfee_r),
            "ordertype": ordertype,
            "minsize": str(minsize),
        }

    def start_maker(self, **offer):
        endpoint = f"/wallet/{self.walletname}/maker/start"
        try:
            return self._rpc("POST", endpoint, json_data=self._maker_payload(**offer))
        except JoinmarketConflictException as e:
            print("Could not start maker without confirmed balance")
            return e.response

    async def start_maker_async(self, **offer):
        endpoint = f"/wallet/{self.walletname}/maker/start"
        try:
            return await self._rpc_async("POST", endpoint, json_data=self._maker_payload(**offer))
        except JoinmarketConflictException as e:
            print("Could not start maker without confirmed balance")
            return e.response

    # ------------------------------------------------------------------ tumbler

    def create_tumbler_plan(self, destinations, parameters, force=True):
        endpoint = f"/wallet/{self.walletname}/tumbler/plan"
        json_data = {"destinations": destinations, "parameters": parameters, "force": force}
        return self._rpc("POST", endpoint, json_data=json_data, timeout=120)

    async def create_tumbler_plan_async(self, destinations, parameters, force=True):
        endpoint = f"/wallet/{self.walletname}/tumbler/plan"
        json_data = {"destinations": destinations, "parameters": parameters, "force": force}
        return await self._rpc_async("POST", endpoint, json_data=json_data, timeout=120)

    def start_tumbler(self):
        return self._rpc("POST", f"/wallet/{self.walletname}/tumbler/start")

    async def start_tumbler_async(self):
        return await self._rpc_async("POST", f"/wallet/{self.walletname}/tumbler/start")

    def get_tumbler_status(self):
        return self._rpc("GET", f"/wallet/{self.walletname}/tumbler/status")

    async def get_tumbler_status_async(self):
        return await self._rpc_async("GET", f"/wallet/{self.walletname}/tumbler/status")

    def stop_tumbler(self):
        return self._rpc("POST", f"/wallet/{self.walletname}/tumbler/stop")

    # ------------------------------------------------------------------ ground truth

    def get_history(self):
        response = self._rpc("GET", f"/wallet/{self.walletname}/history")
        return response.get("history", [])

    def get_logs(self) -> str:
        # Deliberately bypasses _rpc: /logs returns plain text and _rpc always
        # ends in response.json(), so it would raise JSONDecodeError through all
        # four retries. The cost is losing _rpc's 401 -> unlock_wallet -> retry
        # path, so this call fails outright if the token expired during the run;
        # the caller in export_ground_truth catches it. Acceptable because the
        # same log is also collected by the log_src_path directory download
        # (run.sh tees jmwalletd output into $DATA_DIR/logs/jmwalletd.log) --
        # this HTTP copy is the fallback for when the pod is already gone.
        # Proper fix would be a text/raw mode on _rpc.
        url = f"https://{self.host}:{self.port}/api/v1/logs"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            proxies=dict(https=self.proxy) if self.proxy else None,
            timeout=60,
            verify=False,
        )
        response.raise_for_status()
        return response.text

    def export_ground_truth(self, client_path):
        exports = {
            "history.json": self.get_history,
            "session.json": lambda: self.last_session,
        }
        if self.last_plan is not None:
            exports["tumbler_plan.json"] = lambda: self.last_plan
        for filename, producer in exports.items():
            try:
                with open(os.path.join(client_path, filename), "w") as f:
                    json.dump(producer(), f, indent=2)
                print(f"- stored {self.name} {filename}")
            except Exception as e:
                print(f"- could not store {self.name} {filename}: {e}")
        try:
            with open(os.path.join(client_path, "daemon_logs.txt"), "w") as f:
                f.write(self.get_logs())
        except Exception as e:
            print(f"- could not store {self.name} daemon logs: {e}")


class NGMakerClient(MakerClient, JoinMarketNGClient):
    pass


class NGTakerClient(TakerClient, JoinMarketNGClient):
    pass


class NGTumblerTakerClient(JoinMarketNGClient):
    """
    Runs one persistent tumbler plan (optionally with maker interludes) and
    counts each completed taker-coinjoin phase as a round.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        options = dict(self.tumbler_options or {})
        self.address_count = int(options.get("address_count", options.get("addrcount", 3)))
        self.restart_on_failure = bool(options.get("restart", True))
        self.max_replans = int(options.get("max_replans", 3))
        # Tumble repeatedly: after a plan completes, build a fresh one from whatever the coins
        # have become. 0 means keep mixing for the whole simulation.
        self.max_plans = int(options.get("max_plans", 0))
        self.plan_parameters = {k: v for k, v in options.items() if k not in TUMBLER_LOCAL_OPTIONS}
        # A wallet whose UTXOs stay locked after a failed phase keeps rejecting plan creation
        # ("Wallet has no confirmed coins to tumble", then 500s). Without a bound the client
        # retries every update for the whole run and contributes nothing, silently.
        self.max_plan_start_failures = int(options.get("max_plan_start_failures", 20))
        self.plan_start_failures = 0
        self.plan_started = False
        self.finished = False
        self.failed = False
        self.replans = 0
        self.completed_plans = 0
        self.completed_phases = set()

    def is_paused(self, current_block):
        return self.finished or super().is_paused(current_block)

    def _plan_destinations(self):
        return [self.get_new_address() for _ in range(self.address_count)]

    def _on_plan_start_failed(self, error):
        """Count consecutive plan-creation failures and stop once they look terminal."""
        self.plan_start_failures += 1
        print(f"- could not start tumbler plan for {self.name} "
              f"({self.plan_start_failures}/{self.max_plan_start_failures}): {error}")
        if self.plan_start_failures >= self.max_plan_start_failures:
            self.finished = True
            self.failed = True
            print(f"!! {self.name} STUCK: {self.plan_start_failures} consecutive plan-creation "
                  f"failures, giving up. Wallet likely holds UTXOs locked by a failed phase; "
                  f"last error: {error}")

    def _on_plan_started(self, plan):
        self.plan_start_failures = 0
        self.last_plan = plan
        self.plan_started = True
        self.completed_phases = set()
        self.coinjoin_in_process = True
        kinds = [_norm(p.get("kind")) for p in plan.get("phases", [])]
        print(f"Started tumbler plan for {self.name}: {len(kinds)} phases "
              f"({kinds.count('taker_coinjoin')} coinjoins, {kinds.count('maker_session')} maker sessions)")

    def _process_status(self, plan, current_block, current_round) -> int:
        self.last_plan = plan
        delta = 0
        for phase in plan.get("phases", []):
            index = phase.get("index")
            if (_norm(phase.get("kind")) == "taker_coinjoin"
                    and _norm(phase.get("status")) == "completed"
                    and index not in self.completed_phases):
                self.completed_phases.add(index)
                self.completed_coinjoins += 1
                delta += 1
                print(f"Coinjoin for {self.name} completed (phase {index}, txid {phase.get('txid')})")
        if delta:
            print(f"- coinjoin rounds: {current_round + delta} (block {current_block})".ljust(60))

        status = _norm(plan.get("status"))
        if status in TERMINAL_PLAN_STATES:
            self.coinjoin_in_process = False
            if status == "completed":
                self.completed_plans += 1
                limit = f"/{self.max_plans}" if self.max_plans else ""
                print(f"Tumbler plan for {self.name} completed "
                      f"({self.completed_plans}{limit})")
                if self.max_plans and self.completed_plans >= self.max_plans:
                    self.finished = True
                else:
                    # Tumble again from the mixdepths the previous plan landed in. The replan
                    # budget is per-plan, so a fresh plan starts with its retries restored.
                    self.plan_started = False
                    self.replans = 0
            elif self.restart_on_failure and self.replans < self.max_replans:
                self.replans += 1
                self.plan_started = False
                print(f"Tumbler plan for {self.name} {status} ({plan.get('error')}); "
                      f"re-planning ({self.replans}/{self.max_replans})")
            else:
                self.finished = True
                print(f"Tumbler plan for {self.name} {status} ({plan.get('error')}); giving up")
        return delta

    def update(self, current_block, current_round) -> int:
        response = self.update_status() or {}
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)
        if self.finished:
            return 0
        if not self.plan_started:
            if self.is_paused(current_block):
                return 0
            try:
                plan = self.create_tumbler_plan(self._plan_destinations(), self.plan_parameters)
                self.start_tumbler()
                self._on_plan_started(plan)
            except Exception as e:
                self._on_plan_start_failed(e)
            return 0
        try:
            plan = self.get_tumbler_status()
        except Exception as e:
            print(f"- could not fetch tumbler status for {self.name}: {e}")
            return 0
        return self._process_status(plan, current_block, current_round)

    async def update_async(self, current_block, current_round) -> int:
        response = await self.update_status_async()
        self.coinjoin_in_process = response.get("coinjoin_in_process", False)
        if self.finished:
            return 0
        if not self.plan_started:
            if self.is_paused(current_block):
                return 0
            try:
                plan = await self.create_tumbler_plan_async(self._plan_destinations(), self.plan_parameters)
                await self.start_tumbler_async()
                self._on_plan_started(plan)
            except Exception as e:
                self._on_plan_start_failed(e)
            return 0
        try:
            plan = await self.get_tumbler_status_async()
        except Exception as e:
            print(f"- could not fetch tumbler status for {self.name}: {e}")
            return 0
        return self._process_status(plan, current_block, current_round)

    def stop_coinjoin(self):
        try:
            if self.plan_started and self.coinjoin_in_process:
                return self.stop_tumbler()
            print("No tumbler plan running")
            return True
        except Exception as e:
            print(f"Failed to stop tumbler: {e}")
            return False
