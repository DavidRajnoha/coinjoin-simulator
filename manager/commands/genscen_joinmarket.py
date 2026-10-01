#!/usr/bin/env python3
"""
JoinMarket Configuration Generator
Generates customizable JoinMarket simulation configurations with takers and makers
"""

import json
import random
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import argparse

class OfferType(Enum):
    ABSOLUTE = "sw0absoffer"
    RELATIVE = "sw0reloffer"


@dataclass
class FeeConfig:
    """Configuration for maker fees"""
    min_absolute: int = 1000  # satoshis
    max_absolute: int = 5000  # satoshis
    min_relative: float = 0.0001  # 0.01%
    max_relative: float = 0.004  # 0.4%


@dataclass
class QuantileFeeConfig:
    """Quantile-based configuration for maker fees"""
    absolute_quantiles: List[int] = field(default_factory=lambda: [1000, 2000, 3000, 4000, 5000, 6000])  # 0%, 20%, 40%, 60%, 80%, 100%
    relative_quantiles: List[float] = field(default_factory=lambda: [0.0001, 0.001, 0.002, 0.003, 0.004, 0.005])  # 0%, 20%, 40%, 60%, 80%, 100%


@dataclass
class WalletConfig:
    """Configuration for wallet generation"""
    min_utxos: int = 9
    max_utxos: int = 11
    min_total_btc: float = 1.0
    max_total_btc: float = 10.0
    min_utxo_size: int = 100000  # satoshis


@dataclass
class QuantileWalletConfig:
    """Quantile-based configuration for wallet generation"""
    min_utxos: int = 9
    max_utxos: int = 11
    btc_quantiles: List[float] = field(default_factory=lambda: [1.0, 2.5, 4.0, 6.0, 8.5, 10.0])  # 0%, 20%, 40%, 60%, 80%, 100%
    min_utxo_size: int = 100000  # satoshis


@dataclass
class QuantileBondConfig:
    """Quantile-based configuration for fidelity bonds"""
    amount_quantiles: List[int] = field(default_factory=lambda: [10000, 25000, 50000, 75000, 100000, 150000])  # 0%, 20%, 40%, 60%, 80%, 100%


@dataclass
class TumblerOptions:
    """Default tumbler options for takers"""
    addrcount: int = 3
    minmakercount: int = 4
    makercountrange: List[int] = field(default_factory=lambda: [9, 1])
    mixdepthcount: int = 4
    mintxcount: int = 2
    txcountparams: List[int] = field(default_factory=lambda: [2, 1])
    timelambda: int = 60
    stage1_timelambda_increase: int = 3
    liquiditywait: int = 60
    waittime: int = 20
    mixdepthsrc: int = 0
    restart: bool = True
    schedulefile: str = "TUMBLE.schedule"
    mincjamount: int = 100000
    amtmixdepths: int = 4
    rounding_chance: float = 0.25
    rounding_sigfig_weights: List[int] = field(default_factory=lambda: [55, 15, 25, 65, 40])


class JoinMarketConfigGenerator:
    def __init__(self):
        self.fee_config = FeeConfig()
        self.wallet_config = WalletConfig()
        self.tumbler_options = TumblerOptions()

    def generate_utxos(self, total_btc: float, num_utxos: int) -> List[int]:
        """Generate UTXO distribution for a wallet"""
        total_sats = int(total_btc * 100_000_000)

        # Ensure we have enough for minimum UTXO sizes
        min_total = num_utxos * self.wallet_config.min_utxo_size
        if total_sats < min_total:
            total_sats = min_total

        # Generate random distribution
        utxos = []
        remaining = total_sats

        for i in range(num_utxos - 1):
            # Leave enough for remaining UTXOs
            max_utxo = remaining - (num_utxos - i - 1) * self.wallet_config.min_utxo_size
            min_utxo = self.wallet_config.min_utxo_size

            if max_utxo > min_utxo:
                utxo = random.randint(min_utxo, min(max_utxo, int(total_sats * 0.3)))
            else:
                utxo = min_utxo

            utxos.append(utxo)
            remaining -= utxo

        # Add remaining amount as last UTXO
        if remaining >= self.wallet_config.min_utxo_size:
            utxos.append(remaining)
        else:
            # Adjust last UTXO to meet minimum
            utxos[-1] -= self.wallet_config.min_utxo_size - remaining
            utxos.append(self.wallet_config.min_utxo_size)

        random.shuffle(utxos)
        return utxos

    def create_taker_wallet(self, delay_blocks: int = 0) -> Dict[str, Any]:
        """Create a taker wallet configuration"""
        num_utxos = random.randint(self.wallet_config.min_utxos, self.wallet_config.max_utxos)
        total_btc = random.uniform(self.wallet_config.min_total_btc, self.wallet_config.max_total_btc)

        wallet = {
            "funds": self.generate_utxos(total_btc, num_utxos),
            "type": "taker",
            "tumbler_options": {
                "addrcount": self.tumbler_options.addrcount,
                "minmakercount": self.tumbler_options.minmakercount,
                "makercountrange": self.tumbler_options.makercountrange.copy(),
                "mixdepthcount": self.tumbler_options.mixdepthcount,
                "mintxcount": self.tumbler_options.mintxcount,
                "txcountparams": self.tumbler_options.txcountparams.copy(),
                "timelambda": self.tumbler_options.timelambda,
                "stage1_timelambda_increase": self.tumbler_options.stage1_timelambda_increase,
                "liquiditywait": self.tumbler_options.liquiditywait,
                "waittime": self.tumbler_options.waittime,
                "mixdepthsrc": self.tumbler_options.mixdepthsrc,
                "restart": self.tumbler_options.restart,
                "schedulefile": self.tumbler_options.schedulefile,
                "mincjamount": self.tumbler_options.mincjamount,
                "amtmixdepths": self.tumbler_options.amtmixdepths,
                "rounding_chance": self.tumbler_options.rounding_chance,
                "rounding_sigfig_weights": self.tumbler_options.rounding_sigfig_weights.copy()
            }
        }

        if delay_blocks > 0:
            wallet["delay_blocks"] = delay_blocks

        return wallet

    def create_maker_wallet(self, offer_type: OfferType, fee_value: Optional[float] = None) -> Dict[str, Any]:
        """Create a maker wallet configuration"""
        num_utxos = random.randint(self.wallet_config.min_utxos, self.wallet_config.max_utxos)
        total_btc = random.uniform(self.wallet_config.min_total_btc, self.wallet_config.max_total_btc)

        funds = self.generate_utxos(total_btc, num_utxos)
        total_balance = sum(funds)

        offer = {
            "txfee": 0,
            "ordertype": offer_type.value,
            "minsize": self.wallet_config.min_utxo_size,
            "maxsize": int(total_balance * 0.9)  # 90% of wallet balance
        }

        if offer_type == OfferType.ABSOLUTE:
            if fee_value is None:
                fee_value = random.randint(self.fee_config.min_absolute, self.fee_config.max_absolute)
            offer["cjfee_a"] = int(fee_value)
            offer["cjfee_r"] = 0
        else:  # RELATIVE
            if fee_value is None:
                fee_value = random.uniform(self.fee_config.min_relative, self.fee_config.max_relative)
            offer["cjfee_a"] = 0
            offer["cjfee_r"] = fee_value

        return {
            "funds": funds,
            "type": "maker",
            "offers": [offer]
        }

    def generate_config(self,
                        name: str = "JoinMarket Configuration",
                        num_takers: int = 5,
                        num_makers: int = 35,
                        taker_delays: Optional[List[int]] = None,
                        maker_fee_split: float = 0.5,  # Fraction of makers using absolute fees
                        rounds: int = 0,
                        blocks: int = 1000) -> Dict[str, Any]:
        """Generate complete configuration"""

        if taker_delays is None:
            taker_delays = [0, 30, 60, 90, 120][:num_takers]

        config = {
            "name": name,
            "default_version": "joinmarket",
            "rounds": rounds,
            "blocks": blocks,
            "wallets": []
        }

        # Create takers
        for i in range(num_takers):
            delay = taker_delays[i] if i < len(taker_delays) else 0
            config["wallets"].append(self.create_taker_wallet(delay))

        # Create makers
        num_absolute = int(num_makers * maker_fee_split)
        num_relative = num_makers - num_absolute

        # Absolute fee makers
        for i in range(num_absolute):
            config["wallets"].append(self.create_maker_wallet(OfferType.ABSOLUTE))

        # Relative fee makers with distributed fees
        if num_relative > 0:
            # Create linear distribution from max to min
            relative_fees = []
            for i in range(num_relative):
                fee = self.fee_config.max_relative - (
                        (self.fee_config.max_relative - self.fee_config.min_relative) * i / (num_relative - 1)
                )
                relative_fees.append(fee)

            for fee in relative_fees:
                config["wallets"].append(self.create_maker_wallet(OfferType.RELATIVE, fee))

        return config

    def save_config(self, config: Dict[str, Any], filename: str):
        """Save configuration to JSON file"""
        with open(filename, 'w') as f:
            json.dump(config, f, indent=2)

    def load_and_modify_config(self, filename: str) -> Dict[str, Any]:
        """Load existing configuration for modification"""
        with open(filename, 'r') as f:
            return json.load(f)


def format_name(args):
    # Use a JoinMarket-specific naming convention
    if args.name:
        return args.name
    # Example: tumbler_1_maker_30.json
    if args.tumbler_taker_count > 0:
        return f"tumbler_{args.tumbler_taker_count}_maker_{args.maker_count}.json"
    else:
        return f"taker_{args.taker_count}_maker_{args.maker_count}.json"


def random_partition(total, n):
    # Partition 'total' into 'n' positive random integers (satoshis)
    # Returns a list of n integers summing to total
    import numpy as np
    if n == 1:
        return [total]
    cuts = np.sort(np.random.randint(1, total, n - 1))
    cuts = cuts.tolist()  # convert numpy array to list for easier handling
    parts = [cuts[0]]
    for i in range(1, len(cuts)):
        parts.append(cuts[i] - cuts[i - 1])
    parts.append(total - cuts[-1])
    return parts


def parse_delays(delays_str, count):
    if not delays_str:
        return [0] * count
    delays = [int(x) for x in delays_str.split(",")]
    if len(delays) < count:
        delays += [0] * (count - len(delays))
    return delays[:count]


def parse_list_int(s):
    return [int(x) for x in s.split(",") if x]


def parse_list_float(s):
    return [float(x) for x in s.split(",") if x]


def parse_bool(s):
    return str(s).lower() in ("true", "1", "yes", "y")


def sample_from_quantiles(quantiles: List[float], count: int) -> List[float]:
    """Sample values from quantile distribution"""
    if len(quantiles) != 6:
        raise ValueError("Quantiles must have exactly 6 values (0%, 20%, 40%, 60%, 80%, 100%)")

    # Ensure quantiles are sorted
    if not all(quantiles[i] <= quantiles[i+1] for i in range(len(quantiles)-1)):
        raise ValueError("Quantiles must be in ascending order")

    samples = []
    for _ in range(count):
        # Random percentile between 0 and 1
        percentile = random.random()

        # Find which quantile range this percentile falls into
        if percentile <= 0.2:
            # Between 0% and 20%
            t = percentile / 0.2
            value = quantiles[0] + t * (quantiles[1] - quantiles[0])
        elif percentile <= 0.4:
            # Between 20% and 40%
            t = (percentile - 0.2) / 0.2
            value = quantiles[1] + t * (quantiles[2] - quantiles[1])
        elif percentile <= 0.6:
            # Between 40% and 60%
            t = (percentile - 0.4) / 0.2
            value = quantiles[2] + t * (quantiles[3] - quantiles[2])
        elif percentile <= 0.8:
            # Between 60% and 80%
            t = (percentile - 0.6) / 0.2
            value = quantiles[3] + t * (quantiles[4] - quantiles[3])
        else:
            # Between 80% and 100%
            t = (percentile - 0.8) / 0.2
            value = quantiles[4] + t * (quantiles[5] - quantiles[4])

        samples.append(value)

    return samples


def sample_from_int_quantiles(quantiles: List[int], count: int) -> List[int]:
    """Sample integer values from quantile distribution"""
    float_samples = sample_from_quantiles([float(q) for q in quantiles], count)
    return [int(round(sample)) for sample in float_samples]


def generate_fidelity_bond_config_quantile(args, quantile_config: QuantileBondConfig):
    """Generate fidelity bond configuration using quantile distribution"""
    if not args.enable_fidelity_bonds:
        return None

    import datetime

    # Calculate locktime in YYYY-MM format (JoinMarket API format)
    months = random.randint(args.bond_min_locktime_months, args.bond_max_locktime_months)
    current_date = datetime.datetime.now()

    # Add months to current date
    future_year = current_date.year
    future_month = current_date.month + months

    # Handle year overflow
    while future_month > 12:
        future_month -= 12
        future_year += 1

    locktime = f"{future_year}-{future_month:02d}"

    # Sample bond amount from quantiles
    amount = sample_from_int_quantiles(quantile_config.amount_quantiles, 1)[0]

    return {
        "enabled": True,
        "amount": amount,
        "locktime": locktime
    }


def generate_fidelity_bond_config(args):
    """Generate fidelity bond configuration for a maker wallet"""
    if not args.enable_fidelity_bonds:
        return None

    import datetime

    # Calculate locktime in YYYY-MM format (JoinMarket API format)
    # Current date + random months
    months = random.randint(args.bond_min_locktime_months, args.bond_max_locktime_months)
    current_date = datetime.datetime.now()

    # Add months to current date
    future_year = current_date.year
    future_month = current_date.month + months

    # Handle year overflow
    while future_month > 12:
        future_month -= 12
        future_year += 1

    locktime = f"{future_year}-{future_month:02d}"

    # Random bond amount
    amount = random.randint(args.bond_min_amount, args.bond_max_amount)

    return {
        "enabled": True,
        "amount": amount,
        "locktime": locktime
    }


def setup_parser(parser: argparse.ArgumentParser):
    parser.add_argument("--name", type=str, help="scenario name")
    parser.add_argument("--maker-count", type=int, default=30, help="number of makers")
    parser.add_argument("--relative-makers", type=int, default=0, help="number of makers with relative offers (sw0reloffer)")
    parser.add_argument("--taker-count", type=int, default=2, help="number of takers")
    parser.add_argument("--tumbler-taker-count", type=int, default=1, help="number of tumbler takers")
    parser.add_argument("--round-count", type=int, default=0, help="number of rounds")
    parser.add_argument("--block-count", type=int, default=0, help="number of blocks")
    parser.add_argument("--taker-delays", type=str, required=False, help="comma-separated block delays for takers, e.g. 0,10,30")
    parser.add_argument("--tumbler-taker-delays", type=str, required=False, help="comma-separated block delays for tumbler takers, e.g. 0,10,20,30")
    parser.add_argument("--taker-max-coinjoins", type=int, default=0, help="max coinjoins for standard takers (0 for unlimited)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--out-dir", type=str, default="scenarios/joinmarket", help="output directory")
    # FeeConfig
    parser.add_argument("--maker-min-absolute-fee", type=int, default=1000, help="minimum absolute fee (satoshis)")
    parser.add_argument("--maker-max-absolute-fee", type=int, default=5000, help="maximum absolute fee (satoshis)")
    parser.add_argument("--maker-min-relative-fee", type=float, default=0.0001, help="minimum relative fee (fraction)")
    parser.add_argument("--maker-max-relative-fee", type=float, default=0.004, help="maximum relative fee (fraction)")
    # WalletConfig
    parser.add_argument("--wallet-min-utxos", type=int, default=9, help="minimum UTXOs per wallet")
    parser.add_argument("--wallet-max-utxos", type=int, default=11, help="maximum UTXOs per wallet")
    parser.add_argument("--wallet-min-total-btc", type=float, default=1.0, help="minimum total BTC per wallet")
    parser.add_argument("--wallet-max-total-btc", type=float, default=10.0, help="maximum total BTC per wallet")
    parser.add_argument("--wallet-min-utxo-size", type=int, default=100000, help="minimum UTXO size (satoshis)")
    # TumblerOptions
    parser.add_argument("--tumbler-addrcount", type=int, default=3, help="tumbler option: addrcount")
    parser.add_argument("--tumbler-minmakercount", type=int, default=4, help="tumbler option: minmakercount")
    parser.add_argument("--tumbler-makercountrange", type=str, default="9,1", help="tumbler option: makercountrange, e.g. 9,1")
    parser.add_argument("--tumbler-mixdepthcount", type=int, default=4, help="tumbler option: mixdepthcount")
    parser.add_argument("--tumbler-mintxcount", type=int, default=2, help="tumbler option: mintxcount")
    parser.add_argument("--tumbler-txcountparams", type=str, default="2,1", help="tumbler option: txcountparams, e.g. 2,1")
    parser.add_argument("--tumbler-timelambda", type=int, default=60, help="tumbler option: timelambda")
    parser.add_argument("--tumbler-stage1-timelambda-increase", type=int, default=3, help="tumbler option: stage1_timelambda_increase")
    parser.add_argument("--tumbler-liquiditywait", type=int, default=60, help="tumbler option: liquiditywait")
    parser.add_argument("--tumbler-waittime", type=int, default=20, help="tumbler option: waittime")
    parser.add_argument("--tumbler-mixdepthsrc", type=int, default=0, help="tumbler option: mixdepthsrc")
    parser.add_argument("--tumbler-restart", type=parse_bool, default=True, help="tumbler option: restart (bool)")
    parser.add_argument("--tumbler-schedulefile", type=str, default="TUMBLE.schedule", help="tumbler option: schedulefile")
    parser.add_argument("--tumbler-mincjamount", type=int, default=100000, help="tumbler option: mincjamount")
    parser.add_argument("--tumbler-amtmixdepths", type=int, default=4, help="tumbler option: amtmixdepths")
    parser.add_argument("--tumbler-rounding-chance", type=float, default=0.25, help="tumbler option: rounding_chance")
    parser.add_argument("--tumbler-rounding-sigfig-weights", type=str, default="55,15,25,65,40", help="tumbler option: rounding_sigfig_weights, e.g. 55,15,25,65,40")
    # Fidelity Bond options
    parser.add_argument("--enable-fidelity-bonds", action="store_true", help="enable fidelity bonds for maker wallets")
    parser.add_argument("--bond-percentage-makers", type=float, default=0.5, help="percentage of makers with fidelity bonds (0.0-1.0)")
    parser.add_argument("--bond-min-amount", type=int, default=10000, help="minimum fidelity bond amount (satoshis)")
    parser.add_argument("--bond-max-amount", type=int, default=100000, help="maximum fidelity bond amount (satoshis)")
    parser.add_argument("--bond-min-locktime-months", type=int, default=3, help="minimum bond locktime (months)")
    parser.add_argument("--bond-max-locktime-months", type=int, default=12, help="maximum bond locktime (months)")
    parser.add_argument("--bond-maker-extra-utxos", action="store_true", help="give fidelity bond makers more UTXOs scaled by taker count to handle concurrent coinjoins")
    parser.add_argument("--bond-maker-utxo-multiplier", type=float, default=1.0, help="multiplier for bond maker UTXOs (e.g., 1.5 = 50% more UTXOs than taker count)")

    # Privacy-Enhanced Yield Generator
    parser.add_argument("--privacy-enhanced-percentage", type=float, default=0.0,
                       help="percentage of makers to use privacy-enhanced YG (0.0-1.0, enables PE when > 0)")
    # Optional: Override default factors with custom ranges
    parser.add_argument("--txfee-factor-min", type=float, default=0.2,
                       help="minimum txfee randomization factor (default: 0.2 = ±20%%)")
    parser.add_argument("--txfee-factor-max", type=float, default=0.2,
                       help="maximum txfee randomization factor (default: 0.2 = ±20%%)")
    parser.add_argument("--cjfee-factor-min", type=float, default=0.1,
                       help="minimum cjfee randomization factor (default: 0.1 = ±10%%)")
    parser.add_argument("--cjfee-factor-max", type=float, default=0.1,
                       help="maximum cjfee randomization factor (default: 0.1 = ±10%%)")
    parser.add_argument("--size-factor-min", type=float, default=0.1,
                       help="minimum size randomization factor (default: 0.1 = ±10%%)")
    parser.add_argument("--size-factor-max", type=float, default=0.1,
                       help="maximum size randomization factor (default: 0.1 = ±10%%)")
    # Optional: Use quantiles instead of ranges
    parser.add_argument("--txfee-factor-quantiles", type=str,
                       help="quantile values for txfee factor (e.g., '0.15,0.2,0.25')")
    parser.add_argument("--cjfee-factor-quantiles", type=str,
                       help="quantile values for cjfee factor (e.g., '0.08,0.1,0.12')")
    parser.add_argument("--size-factor-quantiles", type=str,
                       help="quantile values for size factor (e.g., '0.08,0.1,0.12')")

    # Quantile-based distribution options
    parser.add_argument("--use-quantiles", action="store_true", help="use quantile-based distributions instead of min/max ranges")
    parser.add_argument("--fee-absolute-quantiles", type=str, default="1000,2000,3000,4000,5000,6000",
                       help="absolute fee quantiles (0%,20%,40%,60%,80%,100%) in satoshis")
    parser.add_argument("--fee-relative-quantiles", type=str, default="0.0001,0.001,0.002,0.003,0.004,0.005",
                       help="relative fee quantiles (0%,20%,40%,60%,80%,100%) as fractions")
    parser.add_argument("--wallet-btc-quantiles", type=str, default="1.0,2.5,4.0,6.0,8.5,10.0",
                       help="wallet BTC amount quantiles (0%,20%,40%,60%,80%,100%) for makers")
    parser.add_argument("--taker-btc-quantiles", type=str, default="",
                       help="taker BTC amount quantiles (0%,20%,40%,60%,80%,100%). If not specified, uses wallet-btc-quantiles divided by 3")
    parser.add_argument("--bond-amount-quantiles", type=str, default="10000,25000,50000,75000,100000,150000",
                       help="fidelity bond amount quantiles (0%,20%,40%,60%,80%,100%) in satoshis")


def handler(args):
    print("Generating JoinMarket scenario...")
    scenario = {
        "name": args.name or f"tumbler_{args.tumbler_taker_count}_maker_{args.maker_count}",
        "default_version": "joinmarket",
        "rounds": args.round_count,
        "blocks": args.block_count,
        "wallets": []
    }
    import random
    SATOSHI = 100_000_000
    # AUTOMATIC LIQUIDITY BALANCING:
    # Scale down taker parameters to ensure they have less liquidity than makers
    taker_utxo_divisor = 3
    taker_btc_divisor = 3

    # Parse quantile configurations if using quantiles
    if args.use_quantiles:
        print("Using quantile-based distributions")
        fee_config = QuantileFeeConfig(
            absolute_quantiles=parse_list_int(args.fee_absolute_quantiles),
            relative_quantiles=parse_list_float(args.fee_relative_quantiles)
        )
        wallet_config = QuantileWalletConfig(
            min_utxos=args.wallet_min_utxos,
            max_utxos=args.wallet_max_utxos,
            btc_quantiles=parse_list_float(args.wallet_btc_quantiles),
            min_utxo_size=args.wallet_min_utxo_size
        )
        bond_config = QuantileBondConfig(
            amount_quantiles=parse_list_int(args.bond_amount_quantiles)
        )

        # Parse taker quantiles (if specified, otherwise use maker quantiles / 3)
        if args.taker_btc_quantiles:
            taker_btc_quantiles = parse_list_float(args.taker_btc_quantiles)
        else:
            taker_btc_quantiles = [q / taker_btc_divisor for q in wallet_config.btc_quantiles]

        # Validate quantile inputs
        validation_quantiles = [
            ("absolute fee", fee_config.absolute_quantiles),
            ("relative fee", fee_config.relative_quantiles),
            ("wallet BTC", wallet_config.btc_quantiles),
            ("bond amount", bond_config.amount_quantiles)
        ]

        if args.taker_btc_quantiles:
            validation_quantiles.append(("taker BTC", taker_btc_quantiles))

        for name, quantiles in validation_quantiles:
            if len(quantiles) != 6:
                print(f"ERROR: {name} quantiles must have exactly 6 values (0%,20%,40%,60%,80%,100%)")
                import sys
                sys.exit(1)
            if not all(quantiles[i] <= quantiles[i+1] for i in range(len(quantiles)-1)):
                print(f"ERROR: {name} quantiles must be in ascending order")
                import sys
                sys.exit(1)
    else:
        print("Using traditional min/max distributions")
        fee_config = None
        wallet_config = None
        bond_config = None

    # --- Underpopulation check (Option 2) ---
    makercountrange = parse_list_int(args.tumbler_makercountrange)
    # Use the first value as the main range (as in default_tumbler_options)
    if makercountrange:
        min_makers_required = makercountrange[0] * (args.tumbler_taker_count)
        if args.maker_count < min_makers_required:
            print(f"ERROR: Not enough makers for the scenario. Requested {args.maker_count}, but at least {min_makers_required} are required for taker_count={args.taker_count}, tumbler_taker_count={args.tumbler_taker_count}, makercountrange={makercountrange[0]}.")
            import sys
            sys.exit(1)

    # Taker wallet parameters (automatically scaled down)
    taker_min_utxos = max(1, args.wallet_min_utxos // taker_utxo_divisor)
    taker_max_utxos = max(taker_min_utxos + 1, args.wallet_max_utxos // taker_utxo_divisor)
    taker_min_total_btc = args.wallet_min_total_btc / taker_btc_divisor
    taker_max_total_btc = args.wallet_max_total_btc / taker_btc_divisor


    def default_tumbler_options():
        return {
            "addrcount": args.tumbler_addrcount,
            "minmakercount": args.tumbler_minmakercount,
            "makercountrange": parse_list_int(args.tumbler_makercountrange),
            "mixdepthcount": args.tumbler_mixdepthcount,
            "mintxcount": args.tumbler_mintxcount,
            "txcountparams": parse_list_int(args.tumbler_txcountparams),
            "timelambda": args.tumbler_timelambda,
            "stage1_timelambda_increase": args.tumbler_stage1_timelambda_increase,
            "liquiditywait": args.tumbler_liquiditywait,
            "waittime": args.tumbler_waittime,
            "mixdepthsrc": args.tumbler_mixdepthsrc,
            "restart": args.tumbler_restart,
            "schedulefile": args.tumbler_schedulefile,
            "mincjamount": args.tumbler_mincjamount,
            "amtmixdepths": args.tumbler_amtmixdepths,
            "rounding_chance": args.tumbler_rounding_chance,
            "rounding_sigfig_weights": parse_list_int(args.tumbler_rounding_sigfig_weights)
        }
    # TAKERS
    taker_delays = parse_delays(args.taker_delays, args.taker_count)
    for idx in range(args.taker_count):
        n_utxos = random.randint(taker_min_utxos, taker_max_utxos)

        if args.use_quantiles:
            total_btc = sample_from_quantiles(taker_btc_quantiles, 1)[0]
        else:
            total_btc = random.uniform(taker_min_total_btc, taker_max_total_btc)

        total_sats = int(total_btc * SATOSHI)
        funds = random_partition(total_sats, n_utxos)
        wallet = {
            "funds": funds,
            "type": "taker",
            "offers": [
                {
                    "mixdepth": 0,
                    "amount_sats": 0,  # 0 means sweep (use all available)
                    "counterparties": 4
                }
            ]
        }
        delay = taker_delays[idx] if idx < len(taker_delays) else 0
        if delay:
            wallet["delay_blocks"] = delay
        if args.taker_max_coinjoins > 0:
            wallet["max_coinjoins"] = args.taker_max_coinjoins
        scenario["wallets"].append(wallet)
    # TUMBLER TAKERS
    tumbler_taker_delays = parse_delays(args.tumbler_taker_delays, args.tumbler_taker_count)
    for idx in range(args.tumbler_taker_count):
        n_utxos = random.randint(taker_min_utxos, taker_max_utxos)

        if args.use_quantiles:
            total_btc = sample_from_quantiles(taker_btc_quantiles, 1)[0]
        else:
            total_btc = random.uniform(taker_min_total_btc, taker_max_total_btc)

        total_sats = int(total_btc * SATOSHI)
        funds = random_partition(total_sats, n_utxos)
        wallet = {
            "funds": funds,
            "type": "taker",
            "tumbler_options": default_tumbler_options()
        }
        delay = tumbler_taker_delays[idx] if idx < len(tumbler_taker_delays) else 0
        if delay:
            wallet["delay_blocks"] = delay
        scenario["wallets"].append(wallet)
    # MAKERS
    abs_makers = args.maker_count - args.relative_makers

    # Determine which makers get fidelity bonds
    total_makers = args.maker_count
    makers_with_bonds = int(total_makers * args.bond_percentage_makers) if args.enable_fidelity_bonds else 0
    bond_indices = set(random.sample(range(total_makers), makers_with_bonds)) if makers_with_bonds > 0 else set()

    # Calculate UTXO requirements for bond makers to handle concurrent taker requests
    total_taker_count = args.taker_count + args.tumbler_taker_count
    if args.bond_maker_extra_utxos and makers_with_bonds > 0:
        # Each bond maker should have at least enough UTXOs to handle multiple concurrent takers
        # Formula: max(4, total_takers * multiplier / num_bond_makers)
        bond_maker_min_utxos = max(int(total_taker_count * 1.5), int(total_taker_count * args.bond_maker_utxo_multiplier / makers_with_bonds))
        # Calculate total UTXOs needed for all bond makers
        total_bond_utxos = bond_maker_min_utxos * makers_with_bonds
        # Calculate liquidity multiplier to maintain proportional liquidity
        regular_maker_avg_utxos = (args.wallet_min_utxos + args.wallet_max_utxos) / 2
        bond_maker_liquidity_multiplier = bond_maker_min_utxos / regular_maker_avg_utxos
        print(f"Bond maker UTXO configuration:")
        print(f"  - Total takers: {total_taker_count}")
        print(f"  - Bond makers: {makers_with_bonds}")
        print(f"  - UTXOs per bond maker: {bond_maker_min_utxos}")
        print(f"  - Liquidity multiplier: {bond_maker_liquidity_multiplier:.2f}x")
    else:
        bond_maker_min_utxos = args.wallet_min_utxos
        bond_maker_liquidity_multiplier = 1.0

    for idx in range(abs_makers):
        # Check if this maker gets a fidelity bond and extra UTXOs
        is_bond_maker = idx in bond_indices
        if is_bond_maker and args.bond_maker_extra_utxos:
            n_utxos = bond_maker_min_utxos
        else:
            n_utxos = random.randint(args.wallet_min_utxos, args.wallet_max_utxos)

        if args.use_quantiles:
            total_btc = sample_from_quantiles(wallet_config.btc_quantiles, 1)[0]
            cjfee_a = sample_from_int_quantiles(fee_config.absolute_quantiles, 1)[0]
        else:
            total_btc = random.uniform(args.wallet_min_total_btc, args.wallet_max_total_btc)
            cjfee_a = random.randint(args.maker_min_absolute_fee, args.maker_max_absolute_fee)

        # Apply liquidity multiplier for bond makers to avoid dilution
        if is_bond_maker and args.bond_maker_extra_utxos:
            total_btc *= bond_maker_liquidity_multiplier

        total_sats = int(total_btc * SATOSHI)
        funds = random_partition(total_sats, n_utxos)
        wallet = {
            "funds": funds,
            "type": "maker",
            "offers": [
                {
                    "txfee": 0,
                    "cjfee_a": cjfee_a,
                    "cjfee_r": 0,
                    "ordertype": "sw0absoffer",
                    "minsize": args.wallet_min_utxo_size,
                    "maxsize": int(sum(funds) * 0.9)
                }
            ]
        }

        # Add fidelity bond configuration if this maker is selected for bonds
        if idx in bond_indices:
            if args.use_quantiles:
                bond_config_obj = generate_fidelity_bond_config_quantile(args, bond_config)
            else:
                bond_config_obj = generate_fidelity_bond_config(args)
            if bond_config_obj:
                wallet["fidelity_bond"] = bond_config_obj

        scenario["wallets"].append(wallet)
    for idx in range(args.relative_makers):
        maker_idx = abs_makers + idx  # Adjust index for relative makers

        # Check if this maker gets a fidelity bond and extra UTXOs
        is_bond_maker = maker_idx in bond_indices
        if is_bond_maker and args.bond_maker_extra_utxos:
            n_utxos = bond_maker_min_utxos
        else:
            n_utxos = random.randint(args.wallet_min_utxos, args.wallet_max_utxos)

        if args.use_quantiles:
            total_btc = sample_from_quantiles(wallet_config.btc_quantiles, 1)[0]
            cjfee_r = round(sample_from_quantiles(fee_config.relative_quantiles, 1)[0], 6)
        else:
            total_btc = random.uniform(args.wallet_min_total_btc, args.wallet_max_total_btc)
            cjfee_r = round(random.uniform(args.maker_min_relative_fee, args.maker_max_relative_fee), 6)

        # Apply liquidity multiplier for bond makers to avoid dilution
        if is_bond_maker and args.bond_maker_extra_utxos:
            total_btc *= bond_maker_liquidity_multiplier

        total_sats = int(total_btc * SATOSHI)
        funds = random_partition(total_sats, n_utxos)
        wallet = {
            "funds": funds,
            "type": "maker",
            "offers": [
                {
                    "txfee": 0,
                    "cjfee_a": 0,
                    "cjfee_r": cjfee_r,
                    "ordertype": "sw0reloffer",
                    "minsize": args.wallet_min_utxo_size,
                    "maxsize": int(sum(funds) * 0.9)
                }
            ]
        }

        # Add fidelity bond configuration if this maker is selected for bonds
        if maker_idx in bond_indices:
            if args.use_quantiles:
                bond_config_obj = generate_fidelity_bond_config_quantile(args, bond_config)
            else:
                bond_config_obj = generate_fidelity_bond_config(args)
            if bond_config_obj:
                wallet["fidelity_bond"] = bond_config_obj

        scenario["wallets"].append(wallet)

    # Apply privacy-enhanced factors to makers if enabled
    if args.privacy_enhanced_percentage > 0:
        maker_wallets = [w for w in scenario["wallets"] if w["type"] == "maker"]
        total_makers = len(maker_wallets)
        num_privacy_makers = int(total_makers * args.privacy_enhanced_percentage)

        if num_privacy_makers > 0:
            # Randomly select which makers get privacy-enhanced mode
            privacy_indices = set(random.sample(range(total_makers), num_privacy_makers))

            print(f"Privacy-enhanced makers: {num_privacy_makers}/{total_makers} " +
                  f"({args.privacy_enhanced_percentage*100:.0f}%)")

            # Parse quantiles if provided
            txfee_quantiles = None
            cjfee_quantiles = None
            size_quantiles = None

            if args.use_quantiles:
                if args.txfee_factor_quantiles:
                    txfee_quantiles = parse_list_float(args.txfee_factor_quantiles)
                if args.cjfee_factor_quantiles:
                    cjfee_quantiles = parse_list_float(args.cjfee_factor_quantiles)
                if args.size_factor_quantiles:
                    size_quantiles = parse_list_float(args.size_factor_quantiles)

            for idx, wallet in enumerate(maker_wallets):
                if idx in privacy_indices:
                    for offer in wallet["offers"]:
                        # Add txfee_factor
                        if txfee_quantiles:
                            offer["txfee_factor"] = sample_from_quantiles(txfee_quantiles, 1)[0]
                        else:
                            offer["txfee_factor"] = random.uniform(
                                args.txfee_factor_min, args.txfee_factor_max)

                        # Add cjfee_factor
                        if cjfee_quantiles:
                            offer["cjfee_factor"] = sample_from_quantiles(cjfee_quantiles, 1)[0]
                        else:
                            offer["cjfee_factor"] = random.uniform(
                                args.cjfee_factor_min, args.cjfee_factor_max)

                        # Add size_factor
                        if size_quantiles:
                            offer["size_factor"] = sample_from_quantiles(size_quantiles, 1)[0]
                        else:
                            offer["size_factor"] = random.uniform(
                                args.size_factor_min, args.size_factor_max)

            print(f"  Default factors: txfee={args.txfee_factor_min}-{args.txfee_factor_max}, " +
                  f"cjfee={args.cjfee_factor_min}-{args.cjfee_factor_max}, " +
                  f"size={args.size_factor_min}-{args.size_factor_max}")

    # Calculate and display bond statistics
    wallets_with_bonds = [w for w in scenario["wallets"] if w.get("fidelity_bond", {}).get("enabled", False)]
    total_bond_amount = sum(w["fidelity_bond"]["amount"] for w in wallets_with_bonds)
    total_wallet_funds = sum(sum(w["funds"]) for w in scenario["wallets"])

    print(f"- requires {(total_wallet_funds / 100_000_000):0.8f} BTC for wallet funds")
    if wallets_with_bonds:
        print(f"- created {len(wallets_with_bonds)} fidelity bonds ({len(wallets_with_bonds)/len([w for w in scenario['wallets'] if w['type'] == 'maker'])*100:.1f}% of makers)")
        print(f"- requires {(total_bond_amount / 100_000_000):0.8f} BTC for fidelity bonds")
        print(f"- total funding requirement: {((total_wallet_funds + total_bond_amount) / 100_000_000):0.8f} BTC")
    import os
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, scenario["name"])
    if not out_path.endswith(".json"):
        out_path += ".json"
    if os.path.exists(out_path) and not args.force:
        print(f"- file {out_path} already exists")
        import sys
        sys.exit(1)
    import json
    with open(out_path, "w") as f:
        json.dump(scenario, f, indent=2)
    print(f"- saved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="JoinMarket Configuration Generator")
    setup_parser(parser)
    args = parser.parse_args()
    handler(args)


if __name__ == "__main__":
    main()