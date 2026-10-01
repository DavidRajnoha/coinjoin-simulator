# Campaign 2026 — definitions and run status

Task: `docs/scenario-generation.md`; operating notes: `RUNBOOK.md`. Definitions are produced by
`scenarios/joinmarket/generator_commands/campaign_2026.sh` (run from the repo root with the
project venv on `PATH`). Every definition records `seed` and the full `generator` arguments, and
`manager.py` copies the scenario into each run's archive, so the configuration is recoverable
from results.

- **Seed 1** of a replicated cell is the existing `final_*` run (generated unseeded — not
  reproducible, no `seed` field). New cells get `--seed 1..3`.
- Cells that differ only in counterparty count draw identical markets at equal seed (the
  count consumes no randomness), so Exp. 1 is a paired comparison.
- Exp. 4 seeds 2 and 3 are the baseline's seeds 2 and 3 with only the fees snapped to the grid.

Legend: ✅ run done · 🔄 queued in `batch_main` (#position) · ⬜ defined, not scheduled · ⏸ skipped · ❌ unusable · — not needed

## Needed

| Configuration | Seed 1 | Seed 2 | Seed 3 | Purpose |
|---|---|---|---|---|
| (5,1), 10 | ✅ Oct 2025 | 🔄 #02 | 🔄 #07 | Exp. 1 weak anchor |
| (9,1), 10 | ✅ Oct+Nov 2025 (same file, counted as one) | 🔄 #01 | 🔄 #06 | Exp. 1+2 baseline |
| (15,1), 10 | 🔄 #04 | 🔄 #09 | 🔄 #11 | recommended setting |
| (9,1), 3 | ✅ Oct 2025 | 🔄 #03 | 🔄 #08 | Exp. 2 weak anchor |
| (9,1), 25 | ❌ Nov 2025 run unusable (see below) | ⏸ skipped | ⏸ skipped | Exp. 2 hard anchor — skipped for now |
| (5,1), 3, +50 single takers | 🔄 #05 | 🔄 #10 | ⬜ defined, not scheduled (doc asks for 2) | Exp. 3 |

## Recommended

| Configuration | Seed 1 | Seed 2 | Seed 3 | Note |
|---|---|---|---|---|
| (7,1), 10 | ✅ Oct 2025 | 🔄 #15 | 🔄 #18 | |
| (13,2), 10 | ✅ Oct+Nov 2025 (same file, counted as one) | 🔄 #16 | 🔄 #19 | doc says (13,1); existing run is [13,2], replicated as such |
| (9,1), 5 | ✅ Oct 2025 | 🔄 #17 | 🔄 #20 | |
| (9,1), 10, quantized fees | 🔄 #14 | 🔄 #12 | 🔄 #13 | Exp. 4 (seeds 2/3 pair with baseline seeds 2/3) |

## Optional

| Configuration | Seed 1 | Seed 2 | Seed 3 | Note |
|---|---|---|---|---|
| (11,1), 10 | ✅ Oct+Nov 2025 (same file, counted as one) | 🔄 #21 | 🔄 #23 | |
| (9,1), 15 | ✅ Oct 2025 | 🔄 #22 | 🔄 #24 | |
| (9,1), 3/5/10/15, +50 takers | ✅ ×4 `final_4_injected/` | — | — | supplementary to Exp. 3 |
| (9,1), 10, role switching on/off | not defined | | | Exp. 5 — design question open (see below) |

## Smoke runs (acceptance criterion 2)

| Shape | Scenario | Status |
|---|---|---|
| plain tumbler (reference, IRC) | `campaign_2026/smoke/smoke_plain_tumbler_seed1.json` (generated, not in git) | ✅ passed §3 check (run 1, 2026-09-30_12-56); re-run on the patched image passed (2026-09-30_15-42). Patch verified in-image: failed fallback broadcast now reported as a failed attempt; unpatched image reports nothing |
| delayed single takers | — | same shape ran cleanly 4× in `final_4_injected` (reference); no separate smoke run |
| quantized fees | — | plain-tumbler shape with different fee values; covered by the plain smoke run. Fee collision is checked on results (§5) |
| role switching | — | ⬜ to do before Exp. 5 (must report interlude uptake) |

## Batches

| Batch | Contents | Est. duration | Status |
|---|---|---|---|
| `batch_main` | 24 runs, `#NN` above = position; needed cells complete after #11 | ~22 days (23 100 blocks) | running since 2026-09-30 15:54 UTC, runner `41a6715f` — see RUNBOOK.md |

## Environment differences from the seed-1 runs

- `btc-node` is Bitcoin Core 25.1 (seed 1 ran on 23). Needed for joinmarket-ng; not expected
  to change reference-client behaviour, but it is not literally "everything else constant".
- Reference client configuration in IRC mode is setting-for-setting identical to seed 1
  (only commented-out lines differ).
- Reference tumblers now get 128 Mi instead of 64 Mi: a reference tumbler was OOMKilled after
  ~9 h at 64 Mi in a 1000-block run. Checked: no seed-1 tumbler was restarted (audit below).
- Run archives are now written to the persistent volume (`/app/logs` = `/workspace/logs`).
- **Reference taker patched to recover from a failed fallback broadcast** (mempool conflict).
  Seed-1 tumblers that hit it went silent for the rest of the run (truncated chains, identifiable by
  `Failed to broadcast transaction` in their nick logs); new seeds retry instead
  (`possible mempool conflict` in the logs). Expect longer chains / more coinjoins in affected
  new-seed tumblers than in their seed-1 counterparts. See JOINMARKET.md "Patches".

## Seed-1 run audit (2026-09-30, archives in `logs_download/`)

- No container restarts (OOMKills) in any usable run; every tumbler log starts at the run start.
- No aborted tumbler schedules: every new schedule follows a "Completed successfully the last
  entry" (engine starts a fresh schedule after each finished one).
- 1–6 failed coinjoin attempts per run, retried inside the schedule; nothing extra reaches the
  chain, but they belong in the per-run failed count (§3).
- `2025-11-03_19-48` (9,1)/25: died during startup — 114/168 clients, 0 coinjoins on chain, no
  tumbler activity. Not usable as seed 1; the cell is skipped for now.
- `2025-11-10_06-53` (13,2): tumbler jcs-002 never started (7 of 8 chains). The Oct run is complete.
- Oct and Nov runs of baseline, (11,1), (13,2) used the identical scenario file (same market);
  counted as a single seed-1 run per the campaign decision.
