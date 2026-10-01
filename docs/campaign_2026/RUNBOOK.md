# Campaign 2026 — runbook

How to operate, monitor and interpret the JoinMarket deanonymization campaign
(task: `docs/scenario-generation.md`). Run status per configuration lives in `CHECKLIST.md`.

Scenario definitions are not in git (`scenarios/joinmarket/experiments/` is ignored): they are
regenerated deterministically by `scenarios/joinmarket/generator_commands/campaign_2026.sh` into
`scenarios/joinmarket/experiments/campaign_2026/definitions/`.

## What is running

- **Batch** `batch_main`: 24 scenarios, run sequentially by the scenario runner inside the
  `emulation-manager` pod in namespace `rajnoha-ns`. Started 2026-09-30 15:54 UTC, runner id
  `41a6715f` (also saved in `.run-rajnoha-ns` at the repo root of the machine it was started from).
- **Scenario files** are on the persistent volume at `/workspace/campaign_2026/batch_main/`
  (copies of the generated definitions, prefixed `NN_` for run order), not baked into the manager image.
- **Archives** of finished scenarios: `/app/logs` in the manager pod, which is the persistent
  volume (`/workspace/logs`). They survive pod restarts.
- **Runner console output**: `/tmp/scenario-runners/41a6715f/output.log` in the manager pod. Not
  persistent; grows ~0.85 GB per scenario (~20 GB for the batch, node disk has TBs free).

## Images the campaign runs on

Client pods pull `:latest` with `imagePullPolicy: Always`, so these digests are what every
scenario of the batch must keep using:

| Image | Digest |
|---|---|
| `drajnoha/joinmarket-client-server` | `sha256:4a2926a598c0750c37322eccadf3fb576eee138ce667d13e9eaf1ddf0dcc0461` (includes the broadcast-recovery patch) |
| `drajnoha/btc-node` | `sha256:5cc17e642a57ef753bddf74e39e54019d72ccc3900ff0f024992269aebe0ec96` (Bitcoin Core 25.1) |
| `drajnoha/irc-server` | `sha256:ccc59a8dec40daa608f94daae756ad5a9c2b4714ffc0bdfbebdd7db1faf5dbc9` |
| `drajnoha/emulator-manager` | `sha256:0f78b75707f29df496b2c475611ce406364b0cf7218986a0c85a2e79c98b47b1` (built 2026-09-23; runs the batch runner and `manager.py`) |

The git tag `campaign-2026` marks the source state these correspond to.

## Expected speed

- ~1.3 min per block (~45–47 blocks/h).
- 1000-block scenario ≈ 22–23 h including ~1 h startup/funding and ~15 min log storage.
  550-block Exp. 3 scenarios ≈ 12 h.
- Whole batch ≈ 22 days, ending around 2026-10-22. The six needed cells are complete after
  scenario #11 (~10 days in).
- Scenario 1 at block 726 had 77 real tumbler coinjoins (3–15 per tumbler).

## Checking progress

```fish
cd ~/Code/PycharmProjects/coinjoin-simulator
source .venv/bin/activate.fish
set -x KUBECONFIG ~/Downloads/kuba-cluster.yaml

python manager/remote_cli.py --namespace rajnoha-ns status              # current scenario, completed count
python manager/remote_cli.py --namespace rajnoha-ns logs -f             # live runner output
python manager/remote_cli.py --namespace rajnoha-ns download-logs -n 2  # last N archives
```

- **KUBECONFIG must be `~/Downloads/kuba-cluster.yaml`.** The default `~/.kube/config` holds an
  expired token for the same cluster; the symptom is `403 ... User "system:unauthenticated"`.
- From another directory or machine, pass `--id 41a6715f --batch` instead of relying on
  `.run-rajnoha-ns`.
- `download-logs -n N` takes the newest N run directories **including the one in progress**,
  which is incomplete. Only finished runs have a `.zip` next to their directory; check `status`
  (or `ls /app/logs` in the pod) first.
- Live per-tumbler view during a scenario (tumblers are `jcs-000` … `jcs-007`):
  `kubectl -n rajnoha-ns exec jcs-000 -- grep -c "Txid was" /home/joinmarket/.joinmarket/logs/TUMBLE.log`

## Do not, while the batch runs

- **Restart or roll out the `emulation-manager` deployment** — kills the runner (the batch stops;
  finished archives are kept, the scenario in progress is lost).
- **Push a new `drajnoha/joinmarket-client-server:latest`** (or other simulation images) — client
  pods pull `:latest` with `imagePullPolicy: Always`, so later scenarios would silently run
  different client code than earlier ones.
- **Start another simulation or run `manager.py clean` in `rajnoha-ns`** — cleanup deletes all
  simulation pods, including the batch's.
- **Test other changes (e.g. merged upstream infra work) against the shared images or namespace.**
  Use a different `--image-prefix` (e.g. a `drajnoha/dev-` prefix or another registry) and a
  different namespace for any build, push or test run until the batch has finished.

To stop: `remote_cli.py --namespace rajnoha-ns stop` (whole batch) or `skip` (current scenario).
To resume after a crash: put the not-yet-finished scenario files in a new directory on
`/workspace` and start a new batch on it — there is no resume flag.

## Reading the results correctly

1. **Count coinjoins from the chain or from `Txid was` lines in each tumbler's `TUMBLE.log`.**
   The manager's `coinjoin rounds: N` counter counts schedule changes, not confirmed coinjoins
   (scenario 1: 168 "rounds" vs 77 real coinjoins).
2. **Broadcast-conflict patch (new seeds only).** When two concurrent coinjoins share a maker's
   coin, the losing taker's fallback broadcast fails (`bad-txns-inputs-missingorspent`). Upstream
   JoinMarket then hangs that tumbler for the rest of the run; our reference image is patched
   (JOINMARKET.md "Patches") so it logs `Failed to broadcast transaction` followed by
   `possible mempool conflict` and retries the entry with new makers. Consequences:
   - Seed-1 (2025) tumblers that hit it are truncated chains — identifiable by
     `Failed to broadcast transaction` in their `J5*.log` with no later `Txid was`.
   - New-seed tumblers that hit it keep mixing: more coinjoins and longer chains than the seed-1
     counterpart. Treat this as a known difference when comparing seeds.
   - Conflict counts are not comparable across seeds: a hung seed-1 tumbler could not conflict
     again. Scenario 1 had 11 conflicts across 5 of 8 tumblers, all recovered.
3. **Benign log patterns:**
   - `Schedule entry ... failed after timeout, trying again` — normal retry after a maker timeout
     (up to ~19 per tumbler per scenario). Nothing extra reaches the chain, but these are the
     "failed coinjoins" for the per-run count in §3 of the task doc.
   - `TUMBLE STARTING` right after `Completed successfully the last entry` — the engine starts a
     new schedule after a finished one; not a restart. (Note the wording "the **last** entry";
     counting only "this entry" makes every finished schedule look aborted.)
   - `error starting jcs-NNN: (409)` on `resourcequotas` at startup — parallel pod creations racing
     on the quota counter; the engine retries up to 3× (scenario 1: 31 → 1 → 0 failed).
4. **Environment differences from the seed-1 (2025) runs:**
   - btc-node is Bitcoin Core 25.1 (was 23).
   - Reference tumblers get 128 Mi memory (was 64 Mi; a tumbler was OOMKilled at 64 Mi in a long
     run in Sep 2026, but none of the seed-1 runs were affected).
   - The broadcast-conflict patch above.
   - Reference client configuration in IRC mode is otherwise setting-for-setting identical.
5. **Seed-1 audit (2025 archives in `logs_download/`):**
   - No container restarts and no aborted schedules in any usable run.
   - 1–3 tumblers per run hung after a broadcast conflict (see 2).
   - (9,1)/25 (`2025-11-03_19-48`) died at startup — 114/168 clients, 0 coinjoins — unusable; the
     cell is skipped for now.
   - Baseline (9,1)/10, (11,1), (13,2): the Oct and Nov runs used the identical scenario file
     (same market) and count as **one** seed-1 run.
   - (13,2) Nov run: tumbler jcs-002 never started (7 of 8 chains).
6. **Seeds and pairing:**
   - Seed 1 = the 2025 run (generated unseeded, not reproducible). New definitions use
     `--seed N`, recorded with all generator arguments in `scenario.json`, which is copied into
     every archive.
   - Configurations differing only in counterparty count share an identical market at equal seed.
   - Exp. 4 (quantized fees) seed N = baseline seed N with only maker fees snapped to the grid.
   - (13,2) is replicated as [13,2] although the task doc lists (13,1).
   - Exp. 3 runs seeds 1–2 (seed 3 defined, not scheduled). Exp. 5 is not defined yet.
7. **Where the §3 records come from** (verified on the smoke run): per-transaction attribution
   joins each wallet's `coins.json` (every coin it ever held) with the full transactions in
   `data/btc-node/block_*.json`; tumbler runs from `TUMBLE.log` / `TUMBLE.schedule_*`; maker
   realized fees from `logs/yigen-statement.csv`; offers over time from `data/orderbook/`;
   configuration and seed from `scenario.json`.

## Not yet committed (as of 2026-10-01)

The generator changes (`--seed`, fee grids), `campaign_2026.sh`, the campaign directory, the
JoinMarket patch in `containers/joinmarket-client-server/Dockerfile`, the persistent-logs mount in
`containers/emulator-manager/deployment.yaml`, and the earlier joinmarket-ng / remote CLI work are
all uncommitted on branch `kubernetes-openshift-with-shadowsocks`.
