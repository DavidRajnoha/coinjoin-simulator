# Running Joinmarket Scenarios

This command runs a scenario using Joinmarket with both taker and maker wallets. In this example, the scenario contains two takers (using tumbler mode) and two makers.

```bash
python manager.py --engine joinmarket run --scenario scenarios/joinmarket/taker_2_maker_10.json
```

## Joinmarket Configuration
The environment deployes joinmarket-client-server dockerized implementations. The Joinmarket configuration is set in the `joinmarket-client-server` container. 
The configuration file is located at `/containers/joinmarket-client-server/jmclient.cfg`. 
The images are cached and need to be rebuilt to apply changes to the configuration.

### Changes from the default Joinmarket configuration (that affect the wallet behavior):    
- The `maker_timeout` value is the default 60 seconds (it was 30 seconds until the Aug 2025 reliability changes). This value is also used to calculate the tumbler restart timer. Tumbler restart = maker_timeout * 20 (20 minutes).

### Patches to the JoinMarket code (applied in `containers/joinmarket-client-server/Dockerfile`)
- **Taker recovers from a failed fallback broadcast** (since Sep 2026). When the maker chosen to
  broadcast does not and the taker's own push then fails (typically a mempool conflict: a maker's
  coin was already spent in a concurrent coinjoin), upstream `handle_unbroadcast_transaction` only
  logs `Failed to broadcast transaction` and never finishes the attempt, so a tumbler hangs for the
  rest of the run. The patch reports the attempt as failed, as `Taker.push()` already does, so the
  tumbler logs `possible mempool conflict` and retries the entry with new makers. Runs before
  Sep 2026 do not have it: in the 2025 campaign 1–3 tumblers per 1000-block run stopped this way.
- **Onion messaging in testing mode advertises `NOT-SERVING-ONION`** instead of `127.0.0.1`, so the
  joinmarket-ng directory server accepts reference clients (mixed runs only).

### Supported Features
- Makers running yield generator.
- Takers creating coinjoin transactions (repeated in fixed time intervals).
- Takers using tumbler mode for scheduling coinjoins.
- Sourcing commitments from takers (joinmarket-default).

### Unsupported Features
- Fidelity bonds are not supported at the moment.

## Scenario File
The scenario file follows a structure similar to the Wasabi scenario but with additional fields for individual wallet settings.

### Wallets Configuration

Each wallet configuration is a JSON object with these keys:

- **`funds`**  
  A list indicating the funds available for coinjoins. Each element can be an integer (representing satoshis) or an object with:
  - `value`: Amount (in satoshis) to use.
  - `delay_blocks`: Number of blocks to wait before sending funds.
  - `delay_rounds`: Number of coinjoin rounds to delay fund delivery.

- **`type`**  
  Specifies the wallet role: either `"taker"` or `"maker"`.

- **`offers`**  
  For maker wallets, an array of offer objects defining fee parameters and size limits. (See below for details.)

- **`tumbler_options`**  
  For taker wallets using tumbler mode, this object controls the scheduling and parameters for coinjoin transactions. (See the section below for details.)

### Taker Offers

A taker can also be run individually (without using tumbler scheduling) by specifying parameters such as delay_blocks, time_between_rounds, and an explicit list of offers. In this mode, the wallet directly controls each coinjoin round. For example:

{
  "funds": [3000000],
  "type": "taker",
  "delay_blocks": 10,
  "time_between_rounds": 10,
  "offers": [
    {
      "mixdepth": 0,
      "amount_sats": 40000,
      "counterparties": 4
    }
  ]
}

In this configuration:

    delay_blocks defines the number of blocks to wait before starting.
    time_between_rounds specifies the time (in minutes) between coinjoin rounds.
    offers contains the parameters for each coinjoin (here, using mixdepth 0, sending 40,000 satoshis, and targeting 4 counterparties).


### Maker Offers

Maker wallets include an `offers` field that is an array of offer objects. Each maker offer includes:

- **`txfee`**: Fixed transaction fee component (in satoshis).  
- **`cjfee_a`**: Absolute coinjoin fee (in satoshis).  
- **`cjfee_r`**: Relative coinjoin fee (as a fraction).  
- **`ordertype`**: The order type (e.g., `"sw0reloffer"`).  
- **`minsize`**: Minimum coinjoin size accepted (in satoshis).  
- **`maxsize`**: Maximum coinjoin size accepted (in satoshis).

For example, a maker offer might look like this:

```json
{
  "txfee": 0,
  "cjfee_a": 5000,
  "cjfee_r": 0.00004,
  "ordertype": "sw0reloffer",
  "minsize": 30000,
  "maxsize": 3000000
}
```

### Tumbler Options

For taker wallets running in tumbler mode, the `tumbler_options` object controls how coinjoins are scheduled. Below is a table summarizing each parameter:

| Parameter                    | Basic Concise Description                                           | Joinmarket Default | Used Scenario Default | Used Scenario Note                                                                                           |
|------------------------------|---------------------------------------------------------------------|--------------------|-----------------------|--------------------------------------------------------------------------------------------------------------|
| `addrcount`                  | Number of destination addresses for outputs                         | 3                  | 3                     | –                                                                                                            |
| `minmakercount`              | Minimum maker counterparties per coinjoin                           | 4                  | 4                     | –                                                                                                            |
| `makercountrange`            | Range of makers as `[mean, spread]`                                 | [9, 1]             | [5, 1]                | Reduced from default for smaller simulations                                                                 |
| `mixdepthcount`              | Number of wallet mixdepths used                                     | 4                  | 3                     | –                                                                                                            |
| `mintxcount`                 | Minimum coinjoin transactions per mixdepth                          | 2                  | 2                     | –                                                                                                            |
| `txcountparams`              | Normal distribution parameters for tx count per mixdepth            | [2, 1]             | [3, 1]                | –                                                                                                            |
| `timelambda`                 | Avg. time (minutes) between transactions (exponential distribution) | 60                 | 5                     | As short as possible for swift simulation, yet long enough for 5 blocks to pass (ensuring UTXO confirmation) |
| `stage1_timelambda_increase` | Multiplier for stage 1 wait time vs stage 2                         | 3                  | 1                     | –                                                                                                            |
| `liquiditywait`              | Wait time (seconds) after failed order selection                    | 60                 | 60                    | –                                                                                                            |
| `waittime`                   | Wait time (seconds) for incoming orders                             | 20                 | 20                    | –                                                                                                            |
| `mixdepthsrc`                | Source mixdepth index                                               | 0                  | 0                     | –                                                                                                            |
| `restart`                    | Resume from an existing schedule file                               | false              | true                  | –                                                                                                            |
| `mincjamount`                | Minimum coinjoin amount (satoshis)                                  | 100,000            | 35,000                | Must correspond with makers; if too low, split amounts may trigger insufficient liquidity errors             |
| `amtmixdepths`               | Total number of mixdepths used (deprecated)                         | -1                 | 4                     | –                                                                                                            |
| `rounding_chance`            | Probability of rounding non-sweep coinjoin amounts                  | 0.25               | 0                     | –                                                                                                            |
| `rounding_sigfig_weights`    | Weights for rounding to 1–5 significant figures                     | [55,15,25,65,40]   | [55,15,25,65,45]      | –                                                                                                            |



**Notes:**
- The defaults shown above are for reference. The example scenario below uses the following tumbler settings:
- The values have been tested with reduced block times to 30-45 seconds from the default 30 - 90 seconds to speed up the sourcing commitments. If run with the default block time, the `timelambda` value should be increased.
- (The configuration is hardcoded in the btc-node mine.sh script and the image needs to be rebuilt to change the block time.)
```bash
    sleep $(($RANDOM % 15 + 30)) # Reduced to 30-45 seconds to speed up joinmarket sourcing commitments
```
- The maker_timeout value in the Joinmarket configuration is set to 30 seconds (default 60 seconds). The tumbler restart timer depends on this value. Tumbler restart = maker_timeout * 20 (5 minutes). For short simulations, it is \
recommended to set the timelambda value in a way that restarts will occur as little as possible. 

```json
{
  "addrcount": 3,
  "minmakercount": 4,
  "makercountrange": [5, 1],
  "mixdepthcount": 3,
  "mintxcount": 2,
  "txcountparams": [3, 1],
  "timelambda": 5,
  "stage1_timelambda_increase": 1,
  "liquiditywait": 60,
  "waittime": 20,
  "mixdepthsrc": 0,
  "restart": true,
  "mincjamount": 35000,
  "amtmixdepths": 4,
  "rounding_chance": 0,
  "rounding_sigfig_weights": [55, 15, 25, 65, 45]
}
```

## joinmarket-ng clients

Wallets can run the [joinmarket-ng](https://github.com/joinmarket-ng/joinmarket-ng) implementation instead of the
reference client-server. Select it with `"default_version": "joinmarket-ng"` for the whole scenario, or per wallet with
`"version": "joinmarket-ng"` (mixed populations). Generate scenarios with
`genscen-joinmarket --client-version joinmarket-ng` or `--ng-makers N --ng-takers N --ng-tumbler-takers N`.

### Messaging
- joinmarket-ng has no IRC; its only message channel is a **directory server**. When any NG wallet is present the
  engine starts `jm-directory` (image `joinmarket-ng-directory`, port 5222) and the NG orderbook watcher
  (`joinmarket-ng-obwatcher`, port 8000) instead of `irc-server`.
- Reference clients in such runs are started with `JM_MESSAGING=directory`: `run.sh` assembles `joinmarket.cfg` from
  `joinmarket.base.cfg` + `messaging-onion.cfg`, i.e. `[MESSAGING:onion]` with `regtest_count = 1,1`. That is the
  reference "testing mode": directory nodes are dialled over plain TCP (no Tor). In that mode the reference bot would
  advertise `127.0.0.1:8080` as its location, which the NG directory rejects at handshake (only `*.onion` or
  `NOT-SERVING-ONION` are valid); the reference image therefore carries a one-line patch of `jmdaemon/onionmc.py`
  (see its Dockerfile) so testing-mode bots advertise `NOT-SERVING-ONION`. Nobody can connect to them directly and
  all private messages are relayed through the directory server (a documented fallback on both sides).
- Images to push for the kubernetes driver: `joinmarket-ng`, `joinmarket-ng-directory`, `joinmarket-ng-obwatcher`
  (thin wrappers over `ghcr.io/joinmarket-ng/joinmarket-ng/*:0.39.2`, see `containers/`) and the rebuilt
  `joinmarket-client-server`.

### Configuration
- NG is configured by environment variables (`SECTION__FIELD`, e.g. `TAKER__MINIMUM_MAKERS`). The engine sets regtest
  defaults (`NG_CLIENT_ENV` in `manager/engine/joinmarket_engine.py`); override them with a scenario-level or
  wallet-level `"ng_env": {"TUMBLER__RETRY_DELAY_SECONDS": "10"}`.
- Maker offers: `maxsize` is ignored (NG derives it from the balance). Privacy-enhanced factors (`txfee_factor`,
  `cjfee_factor`, `size_factor`) are applied as `MAKER__*_FACTOR` settings of the container.
- Fidelity bonds work as for the reference client (`fidelity_bond` with `locktime` `YYYY-MM`); the timelock address is
  registered in the wallet and picked up by the maker at start.
- Tumbler takers use NG `TumbleParameters` names in `tumbler_options`: `maker_count_min`, `maker_count_max`,
  `time_lambda_seconds` (seconds, not minutes!), `stage1_wait_multiplier`, `include_maker_sessions` (maker interludes),
  `maker_session_seconds`, `maker_session_idle_timeout_seconds`, `mintxcount`, `mincjamount_sats`, `max_phase_retries`,
  `rounding_chance`, `rounding_sigfig_weights`. Emulator-only keys: `address_count` (destinations, default 3),
  `restart` (re-plan after a failed plan, default true), `max_replans` (default 3), and `max_plans` — how many
  plans to run in sequence, **default 0 = keep tumbling for the whole simulation**. After a plan completes the
  client builds a fresh one from the mixdepths the previous plan landed in (NG's `PlanBuilder` reads current
  balances), with new destination addresses and a restored replan budget. Set `max_plans: 1` for the old
  single-tumble behaviour. Each completed taker-coinjoin phase counts as one round; maker-session phases do not.

### Ground truth
Per NG client the log archive contains, besides `coins.json` / `unspent_coins.json` / `keys.json` /
`fidelity_bonds.json`: `history.json` (`/wallet/{name}/history`), `session.json`, `tumbler_plan.json` (final
`/tumbler/status`, tumblers only), `daemon_logs.txt` (`/logs` ring buffer) and the downloaded data directory
`.joinmarket-ng/` with `history.csv`, `schedules/<wallet>.yaml` (persistent tumbler plan), `fidelity_bonds_<fp>.json`
and `logs/jmwalletd.log` (full daemon output).

`history.json` rows are written at protocol time (`txid`, counterparties, fees). Their `confirmations` / `success`
fields are updated by the bots' own monitoring (makers: every `MAKER__RESCAN_INTERVAL_SEC`; takers: only while a
taker/tumbler run is alive), so the last CoinJoin of a single-shot taker may remain `"Pending confirmation"` in the
archive even though it is mined. Treat the `txid` together with `coins.json` and the `btc-node` blocks as
authoritative for inclusion.

### Client resources
Per-client Kubernetes requests (limits are 1.5x) set in `JoinmarketEngine.start_client`:

| client | CPU | memory | rationale |
|---|---|---|---|
| joinmarket-ng (any role) | 0.1 | 128 Mi | `jmwalletd` idles at ~80 MiB; measured ~119 MiB in a 9 h run |
| reference maker / single-shot taker | 0.05 | 64 Mi | unchanged |
| reference **tumbler** | 0.05 | 128 Mi | the daemon grows to ~91 MiB over a long schedule and was OOMKilled at the old 96 Mi limit (1000-block run, 2026-09-22) |

CPU, not memory, is what caps run size: in a 15 CPU / 39 Gi namespace, ~140 clients saturate the CPU
request quota while using under half the memory.

### Known limitations
- `btc-node` runs Bitcoin Core 25.1 (`lncm/bitcoind:v25.1`): NG needs `listsinceblock ... include_change` (Core >= 25)
  for its transaction monitor and `gettxspendingprevout` (Core >= 24), while the reference client still needs legacy
  (BDB) wallets, which Core 26+ only creates with `-deprecatedrpc=create_bdb`.
- Every NG client loads its own descriptor wallet into the shared `btc-node` (one Core wallet per client).

### Parity with the tuned reference configuration
NG defaults differ from the reference `joinmarket.cfg` in ways that reproduce the "Not enough makers selected" failures
the reference setup was tuned against. The engine therefore sets (see `NG_CLIENT_ENV`):

| reference (`joinmarket.cfg` / YG) | NG setting | NG default | emulator value |
|---|---|---|---|
| `max_cj_fee_abs = 100000` | `TAKER__MAX_CJ_FEE_ABS` | 500 | 100000 |
| `max_cj_fee_rel = 0.01` | `TAKER__MAX_CJ_FEE_REL` | 0.001 | 0.01 |
| no fee quantization | `TAKER__REQUIRE_QUANTIZED_CJ_FEES` | true | false |
| no per-maker input cap | `TAKER__MAX_MAKER_UTXOS` | 15 (maker dropped) | 50 (0 would break sweeps) |
| `minimum_makers = 4` | `TAKER__MINIMUM_MAKERS` | 4 | 4 (NG caps it at `counterparties`) |
| `maker_timeout_sec = 60` | `TAKER__MAKER_TIMEOUT_SEC` | 60 | 60 |
| tumbler `waittime = 20` | `TAKER__ORDERBOOK_MIN_WAIT` | 30 | 20 |
| `bondless_makers_allowance = 0.125` | `TAKER__BONDLESS_MAKERS_ALLOWANCE` | 0.05 | 0.125 |
| makers charge fees without bonds | `TAKER__BONDLESS_REQUIRE_ZERO_FEE` | true | false |
| YG re-announces right after a CoinJoin | `MAKER__OFFER_REANNOUNCE_DELAY_MAX` | 600 s (+ new nick) | 0 |
| plain YG: fixed offer sizes | `MAKER__SIZE_FACTOR` | 0.1 | 0 (offer `size_factor` overrides) |
| YG merges mixdepth-0 UTXOs, `maxsize` = mixdepth balance | `MAKER__ALLOW_MIXDEPTH_ZERO_MERGE` | false (`maxsize` = largest UTXO) | true |
| `taker_utxo_age = 5` (PoDLE) | `TAKER__TAKER_UTXO_AGE`, `TUMBLER__MIN_CONFIRMATIONS_BETWEEN_PHASES` | 5 / 6 | 5 / 5 (do not lower: daemon-launched NG makers verify commitment age with a fixed 5, so an earlier phase fails with "only 0 authenticated makers" and burns its PoDLE indices) |
| `taker_utxo_retries/amtpercent`, `tx_fees = 3`, `tx_fees_factor`, `max_sweep_fee_change`, `tx_broadcast`, `bond_value_exponent`, `max_sats_freeze_reuse` | same names under `TAKER__` / `WALLET__` | identical | (defaults) |

Anything else can be overridden per scenario/wallet with `ng_env`.

Note for mixed scenarios: a *reference* taker aborts with "Not enough counterparties" after `!ioauth` when it collected
fewer makers than the reference `minimum_makers = 4`, so give reference takers `counterparties >= 4` (NG takers cap
`minimum_makers` at the requested `counterparties`).

## Logs

- coins.json, keys.json and unspent_coins.json contain information in wasabi wallet format
- `joinmarket/jmwalletd.log` contains all logs from joinmarket
  - For maker:
    - `obtained tx` in process of the coinjoin
    - `Added utxos:` indicate successful coinjoin
  - For taker:
    - successful coinjoins are logged as `Coinjoin completed correctly`
    - failed coinjoins are logged as `Coinjoin did not complete successfully`.
    - Sources of failure:
      - `Failed to source a commitment`
      - `Makers who didnt respond: [`
  - For tumbler additionaly:
    - Retries: `Stall detected`
    - `NotEnoughFundsException` Remaining funds are lower than the minimum coinjoin amount, the amount gets increased but the taker does not have the funds to cover the increase.
    - `INFO:Failed to source a commitment` Transaction initiated too early, the utxos do not have 5 confirmations.
- `joinmarket/.joinmarket/logs`
  - `JXXXX.log` contains logs for each script run, content same as in `jmwalletd.log`
  - `TUMBLE.log` contains concise information about the tumbler coinjoins
  - `TUMBLE.schedule` contains the schedule for the tumbler, updated
  - `yigen-statement.csv` should contains the yield generator info, does not seem to work properly