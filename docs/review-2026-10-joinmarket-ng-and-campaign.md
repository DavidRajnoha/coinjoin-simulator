# Review notes — joinmarket-ng integration, K8s reliability, remote CLI, campaign 2026

Self-contained summary of the uncommitted work on branch `kubernetes-openshift-with-shadowsocks`
(base commit `8bcfe28`), written so a reviewer with no prior context can check it. Work done
2026-09-18 → 2026-10-01. Sections follow the proposed commit split (§2); each lists what changed,
why, how it was verified, and what to look at critically.

A 24-scenario simulation batch is **running on the cluster from this code right now** (see
`CLAUDE.md`). Do not rebuild/push simulation images, restart the manager deployment, or run
simulations in namespace `rajnoha-ns` while reviewing.

---

## 1. Ownership of the uncommitted changes

Not everything uncommitted was written in this work. At the start (2026-09-18) these were
already modified/untracked — the repository owner's own work in progress:

| Pre-existing change | Files |
|---|---|
| Privacy-enhanced yield generator for makers (`txfee/cjfee/size_factor`) | `manager/commands/genscen_joinmarket.py` (`--privacy-enhanced-*` flags and the block applying factors), `joinmarket_client_base.py` (`start_maker*` factor params), `containers/joinmarket-client-server/Dockerfile` (base image `taker-logs` → `yg-pe-rpc`), `generator_commands/privacy_enhanced.sh` |
| Scenario-runner memory monitor + threaded stdout/stderr streaming | `scenario_runner.py`, `Dockerfile` (`psutil`) |
| Manager pod resources 2Gi/6Gi + persistent volume | `containers/emulator-manager/deployment.yaml`, `containers/emulator-manager/pvc.yaml` |
| Ignore rules | `.gitignore` (`.runner*`, `*.logs`) |
| Scenario scripts | `generator_commands/production_bonds_sybil.sh` (staged), `final_4_injected_takers_test.sh`, `scenarios/joinmarket/limited_takers_experiment.json` |
| Docs | `docs/single-coinjoin-taker-implementation.md`, `docs/scenario-generation.md` (campaign task) |
| Emergency log script | `collect_crashed_simulation_logs.py` — **folded into `manager/collect_logs.py` and deleted** in this work (§2.5) |
| Local artifacts — never commit | `emergency_logs/`, `runner_logs/`, `orderbook.tar.gz`, `.run-rajnoha-ns` |

The pre-existing hunks and the hunks from this work touch different lines in the mixed files
(`genscen_joinmarket.py`, `joinmarket_client_base.py`, the client-server `Dockerfile`,
`scenario_runner.py`, `deployment.yaml`), so they can be committed separately.

---

## 2. Commits

Ordered so upstream can take any prefix: infrastructure first, then JoinMarket changes that work
on the existing Bitcoin Core, then the Core 25.1 bump on its own (it may affect the Wasabi engine
and has not been tested there), then joinmarket-ng, which requires 25.1. The local tag
`campaign-2026` marks the last commit (the source state of the running campaign).

| # | Commit subject | Kind | Section |
|---|---|---|---|
| 1 | chore: scenario runner memory monitor, manager pod resources and PVC | infra (owner's earlier work) | 2.0 |
| 2 | fix(k8s): reliable exec streams, log download and cleanup | infra | 2.3 |
| 3 | feat(engine): per-client log source and ground-truth export hooks | infra | 2.1 (engine hooks) |
| 4 | feat(k8s): keep run archives on the manager PVC | infra | 2.4 |
| 5 | feat: unified remote CLI with batch support and collect-logs | infra | 2.5 |
| 6 | feat(joinmarket): privacy-enhanced yield generator makers, scenario scripts and docs | JoinMarket (owner's earlier work) | 2.0 |
| 7 | fix(joinmarket): give reference tumblers 128 Mi | JoinMarket | 2.3 (last bullet) |
| 8 | feat(genscen): seeded scenarios with provenance and fee grids | JoinMarket | 2.6 |
| 9 | fix(joinmarket): recover tumblers from a failed fallback broadcast | JoinMarket | 2.7 |
| 10 | build(btc-node): Bitcoin Core 25.1 | infra, **risky** | 2.1 (btc-node bullet) |
| 11 | feat(joinmarket): joinmarket-ng clients and mixed NG + reference runs | JoinMarket (+ NG names in shared drivers) | 2.1, 2.2 |
| 12 | docs: campaign 2026 runbook, checklist and definitions script; agent and review notes | docs | 2.8 |

Infrastructure files touched by JoinMarket commits: commit 11 adds the NG image/pod names to the
cleanup lists in `manager/driver/{docker,podman,kubernetes}.py` and the NG log-wait skip in
`kubernetes.py::download`.

The sections below are grouped by topic.

### 2.0 Owner's pre-existing work (commits 1 and 6)
Content of §1, committed unchanged and split into its infrastructure part (commit 1) and its
JoinMarket part (commit 6), so the other commits contain only this work.

### 2.1 joinmarket-ng clients and mixed NG + reference runs
**What.** New client implementation `joinmarket-ng` (NG; `jmwalletd`, JAM-compatible HTTP API)
selectable per wallet (`"version": "joinmarket-ng"`) or per scenario (`default_version`).
- Containers: `containers/joinmarket-ng/` (wrapper over `ghcr.io/joinmarket-ng/joinmarket-ng/jmwalletd:0.39.2`,
  non-root uid 1000, logs teed into the data dir), `joinmarket-ng-directory/`, `joinmarket-ng-obwatcher/`.
- `containers/btc-node/Dockerfile`: Bitcoin Core 23 → **25.1** (`lncm/bitcoind:v25.1`). NG needs
  `listsinceblock … include_change` (Core ≥ 25); on 23 its tx monitor fails every poll.
- Reference client `containers/joinmarket-client-server/`: `joinmarket.cfg` is now a base, and
  `run.sh` appends `messaging-irc.cfg` or `messaging-onion.cfg` according to `JM_MESSAGING`
  (`irc` default | `directory`). The Dockerfile seds `jmdaemon/onionmc.py` to advertise
  `NOT-SERVING-ONION` — testing mode otherwise advertises `127.0.0.1:8080`, which the NG
  directory rejects at handshake.
- `manager/wasabi_clients/joinmarket_clients/joinmarket_ng_clients.py` (new): `NGMakerClient`,
  `NGTakerClient` (MRO reuse of the reference update loops), `NGTumblerTakerClient` (drives
  `/tumbler/plan|start|status|stop`), ground-truth export (`history.json`, `session.json`,
  `tumbler_plan.json`, `daemon_logs.txt`).
- `joinmarket_client_base.py`: `from_wallet(..., version=)` picks the NG or reference class set.
  `joinmarket_clients.py`: `OrderbookWatchClient(supports_refresh=, supports_bonds=)` (NG watcher
  serves only `/orderbook.json`).
- `manager/engine/joinmarket_engine.py`: **directory mode** when any wallet is NG — starts the NG
  directory server (`jm-directory:5222`) and NG orderbook watcher (port 8000) instead of IRC;
  reference clients get `JM_MESSAGING=directory` (testing mode: plain TCP to the directory, no
  Tor). `NG_CLIENT_ENV` holds the NG config as env vars; NG clients 0.1 CPU / 128 Mi.
- `engine_base.py`: per-client `log_src_path` and optional `client.export_ground_truth()` in
  `store_client_logs`.
- Drivers: cleanup name lists include the NG images / `jm-directory`.
- Generator: `--client-version`, `--ng-makers/--ng-takers/--ng-tumbler-takers`, `--ng-tumbler-*`
  options (`NGTumblerOptions`, TumbleParameters names).
- `JOINMARKET.md`: "joinmarket-ng clients" section (messaging, config, parity table, ground truth,
  resources, limitations).

**Why the non-obvious NG settings** (all in `NG_CLIENT_ENV`, table in JOINMARKET.md): NG defaults
reject the generated market ("Not enough makers selected") — fee caps raised to the reference
values (`max_cj_fee_abs=100000`, `max_cj_fee_rel=0.01`), quantized-fee requirement off;
`TAKER__MAX_MAKER_UTXOS` must be > 0 (0 breaks sweep fee sizing) → 50;
`MAKER__ALLOW_MIXDEPTH_ZERO_MERGE=true` (else an NG maker's maxsize = its largest single UTXO,
and the emulator funds makers into mixdepth 0 only); `TAKER__TAKER_UTXO_AGE` and
`TUMBLER__MIN_CONFIRMATIONS_BETWEEN_PHASES` must stay 5 (daemon-launched NG makers verify PoDLE
with a hard-coded age of 5); `MAKER__OFFER_REANNOUNCE_DELAY_MAX=0`. In mixed runs reference takers
need `counterparties >= 4` (reference `minimum_makers=4`).

**Verified.** Local docker: `ng_basic` (NG maker + taker CoinJoin), `ng_tumbler` (plan with maker
interludes to completion), `mixed_basic` (NG taker with reference makers and vice versa). K8s:
140-client mixed production run (12/12 CoinJoins, a cross-implementation tx with 2 NG + 2
reference makers); 3-scenario batch NG → mixed → NG, 3/3 successful.

**Review focus.** Reference/IRC path must be unchanged when no NG wallet exists (assembled IRC
config was diffed setting-by-setting against the old `joinmarket.cfg`: identical). Core 25.1 also
runs the reference-only campaign — a behaviour difference from 2025 runs is not expected but not
proven. `JoinMarketNGClient.get_logs` bypasses `_rpc` (plain-text endpoint; loses token refresh;
the file copy of the same log is the primary source).

### 2.2 NG tumbler: repeated plans and stuck detection
**What.** In `NGTumblerTakerClient`: after a completed plan, build a fresh one (`max_plans`,
default 0 = keep tumbling all run); per-plan replan budget (`max_replans`, default 3) restored for
each new plan; give up loudly (`!! <name> STUCK`) after `max_plan_start_failures` (default 20)
consecutive plan-creation failures. Emulator-only options are stripped from the payload sent to
`/tumbler/plan` (`TUMBLER_LOCAL_OPTIONS`). Generator flag `--ng-tumbler-max-plans`.

**Why.** One NG tumbler retried plan creation 1333 times over 13 h ("Wallet has no confirmed
coins to tumble", then HTTP 500) after a phase failed on locked UTXOs, while its wallet held
0.75 BTC — silently contributing nothing.

**Verified.** 1000-block run: a tumbler completed 12 consecutive plans / 103 taker CoinJoins.
State machine checked offline (counter reset on success, give-up at limit, `max_plans` stop).

**Review focus.** The root cause (NG keeping UTXOs locked after a failed phase) is not fixed, only
detected and stopped.

### 2.3 Kubernetes driver and log-collection reliability
**What** (`manager/driver/kubernetes.py`, `engine_base.py`):
- Per-thread API clients for `kubernetes.stream()` exec calls (`_stream_api`).
- `download()` spools the tar to a temp file instead of memory, always closes the websocket, and
  prints tar's stderr (`!! tar warning …`).
- `_list_with_retry` rebuilds the client once if listing pods/services fails during cleanup.
- Log download pool bounded to `LOG_DOWNLOAD_WORKERS = 8`.
- Skip the reference-client log-file wait for NG data directories (NG never writes those files).
- Reference tumblers get 128 Mi instead of 64 Mi (`joinmarket_engine.start_client`).

**Why.** `stream()` monkey-patches the shared `ApiClient.request`; concurrent downloads made the
websocket transport stick, so `cleanup()` failed (`Error listing pods: (0)`) and left 144
pods/services holding the namespace quota. The manager pod was OOMKilled (6 Gi) buffering whole
tars × all CPUs concurrently. tar warnings were discarded. A reference tumbler was OOMKilled after
~9 h at 64 Mi (daemon reached ~91 MiB).

**Verified.** 140-client run: cleanup complete (140 pods + 140 services deleted, 0 list errors);
1000-block run: 136/136 clients' logs downloaded, no manager OOM. Batch runs since: clean.

**Review focus.** The NG log-wait skip keys on the substring `"joinmarket-ng"` in the source path
— a client detail living in the driver. A finding during review of this: 31 NG clients lacked
`history.csv` — **not** a tar loss (0 tar warnings) but NG writing it only on its periodic rescan;
`history.json` (API export) is complete and canonical.

### 2.4 Persistent run archives
**What.** `deployment.yaml` mounts the PVC subdirectory `logs` at `/app/logs`, where `manager.py`
writes archives (`./logs` relative to `workingDir /app`).

**Why.** Archives were on the container overlay and were lost on every pod restart (a 245 MB
archive of a 1000-block run was lost to a rollout). `/workspace/logs` must exist and be owned by
uid 1000 before first start (created manually on the cluster).

**Verified.** Archives survived a pod restart; writes as uid 1000 work.

### 2.5 Unified remote CLI and discoverable emergency log collection
**What.**
- `manager/remote_cli.py` (new): one entrypoint for cluster runs — `run --scenario X.json`
  (single) or `run --scenario-dir DIR` (batch), plus `status`, `logs [-f]`, `stop`, `skip`,
  `download-logs`, `collect-logs`. Resolves the active run from `.run-<namespace>`, falling back to
  the older `.runner-*` / `.simulation-*` files. Routes to the unchanged implementations in
  `manager_remote.py` / `manager_remote_batch.py`, which now print a pointer to it. `manager.py`
  is deliberately untouched.
- `manager/collect_logs.py` (replaces `collect_crashed_simulation_logs.py`): pod filter now also
  matches `jm-directory`, `joinmarket-obwatch`, `joinmarket-distributor`; `bitcoin-cli`
  credentials fixed from `test:test` to `user:password` (`containers/btc-node/bitcoin.conf`; the
  old script got "Authorization failed" and silently dumped 0 blocks); block dump opt-in
  (`--blocks`).
- `scenario_runner.py`: `--in-cluster` used `default="False"` (a truthy string) → plain
  `store_true`; hardcoded `/home/drajnoha/...` shadowsocks path → relative to the script.
- `README.md`: "Running from an in-cluster orchestrator" section.
- `.gitignore`: `.run-*` (the CLI's run-id files).

**Verified.** `status`/`logs` resolved a live batch with no id; `skip` refuses on a single run;
`collect-logs` collected all 10 pods of a live run; bitcoin-cli fix checked against a live node.

**Review focus.** `download-logs -n N` picks the newest N directories including the run in
progress (incomplete). No resume for an interrupted batch.

### 2.6 Scenario generator: seeds, provenance, fee grids
**What** (`genscen_joinmarket.py`): `--seed N` seeds both `random` and `numpy`; every scenario
records `"seed"` and `"generator"` (all generator arguments). `--fee-absolute-grid` /
`--fee-relative-grid` snap each drawn maker fee to the log-nearest grid value **after** the draw.

**Why.** Campaign replicates must be reproducible and recoverable from the run archive (the
scenario is copied into each archive). Snapping after the draw keeps the random stream identical,
so a quantized scenario at seed N is the continuous one with only fees changed.

**Verified.** Same seed → byte-identical file; different seed → different; new definitions
structurally identical to the 2025 scenarios they replicate; quantized seed 2 = baseline seed 2
except fees (absolute fee values 78 distinct → 6 across 80 makers).

**Review focus.** Counterparty count consumes no randomness, so configurations differing only in
it share a market at equal seed — intended (paired comparison), but worth knowing.

### 2.7 Reference JoinMarket patch: recover from a failed fallback broadcast
**What.** `containers/joinmarket-client-server/Dockerfile` adds
`self.on_finished_callback(False, fromtx=True)` after `Failed to broadcast transaction` in
`jmclient/taker.py::Taker.handle_unbroadcast_transaction` (asserts the target lines exist once,
so an upstream change fails the build). `JOINMARKET.md` "Patches to the JoinMarket code".

**Why.** When the maker chosen to broadcast does not, the taker pushes the transaction itself; on
a mempool conflict (a maker's coin already spent in a concurrent CoinJoin) upstream only logs the
error and never finishes the attempt — the daemon stays "taker running" and the tumbler stops for
the rest of the run. In the 2025 runs 1–3 of 8 tumblers per 1000-block run ended this way.
`Taker.push()` already handles the immediate variant with the same callback; the tumbler then
logs `possible mempool conflict` and retries the entry with new makers.

**Verified.** In-image test forcing the fallback push to fail: patched image reports
`(res=False, fromtx=True)`, unpatched base reports nothing. Live: campaign scenario 1 had 11
broadcast conflicts across 5 of 8 tumblers, all recovered.

**Review focus.** Changes behaviour versus the 2025 runs the campaign compares against
(documented in the campaign runbook). The callback path is upstream's existing failure handler.

### 2.8 Campaign 2026 definitions and docs
**What.** `generator_commands/campaign_2026.sh` (all definitions, deterministic);
`CLAUDE.md` (pointer for agents that a campaign may be running);
`docs/campaign_2026/RUNBOOK.md` + `CHECKLIST.md` (operation, cautions, image digests, result
interpretation, run status); this review document. The scenario JSON itself is not committed
(`scenarios/joinmarket/experiments/` is ignored); it is regenerated by the script. The three NG
test scenarios referenced by JOINMARKET.md (`scenarios/joinmarket/test/{ng_basic,ng_tumbler,mixed_basic}.json`)
are force-added in commit 11 although that directory is ignored.

---

## 3. Verification overview

| Run | Where | Result |
|---|---|---|
| `ng_basic`, `ng_tumbler`, `mixed_basic` | local docker, 2026-09-18 | NG CoinJoin, NG tumbler plan with interludes completed, mixed interop both directions |
| production mixed, 140 clients | K8s | 12/12 CoinJoins incl. cross-implementation |
| overnight, 1000 blocks | K8s | cleanup fix verified; second run verified OOM fix (136/136 logs) |
| batch NG → mixed → NG | K8s, 2026-09-23 | 3/3, clean cleanup between topologies |
| smoke plain tumbler ×2 | K8s, 2026-09-30 | all §3 campaign records reconstructable from the archive; patched image runs normally |
| campaign scenario 1 | K8s, running | 168/168 clients, 77 CoinJoins at block 726, 11 recovered broadcast conflicts |

There is no automated test suite in the repo; the offline checks above (state machine, seed
determinism, grid pairing, in-image patch test) were ad-hoc scripts and are not committed.

## 4. Known limitations / open items

- NG: `history.csv` may be absent if the run ends before NG's rescan — use `history.json`.
- NG: tumbler UTXO-lock root cause not fixed (2.2). NG maker interludes advertise 0-fee bondless
  offers; whether takers pick them often enough for the planned role-switching experiment is
  unmeasured.
- Fidelity bonds: work end-to-end for the reference client but only partly (4 of 40 configured
  bonded makers appeared in the orderbook in the last bond run); `JOINMARKET.md` still says
  unsupported.
- The manager's `coinjoin rounds` counter counts schedule changes, not CoinJoins
  (scenario 1: 168 vs 77).
- Startup `409 Conflict` on `resourcequotas` with many clients is a benign quota-update race; the
  engine's 3× retry absorbs it.
- Three NG Docker Hub repositories (`drajnoha/joinmarket-ng*`) were created public.
