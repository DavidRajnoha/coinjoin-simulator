#!/bin/bash
# Deanonymization campaign (docs/scenario-generation.md, §4): replicate seeds for the
# final_1/final_2/final_4 "quantiles_staggered" family, plus the configurations that
# have no run yet.
#
# Seeds: the existing final_* runs are seed 1 (generated unseeded, so not reproducible).
# New definitions pass --seed N and carry "seed" + "generator" in the scenario JSON,
# which is copied into every run's log archive -> configuration recoverable from results.
# Configurations that differ only in counterparty count consume the same random draws,
# so at equal seed they share an identical market (paired comparison across Exp. 1).
#
# Every flag below is copied from the command that generated the seed-1 run of the same
# configuration; only --seed and the _seedN name suffix are new.
#
# Run from the repository root:  bash scenarios/joinmarket/generator_commands/campaign_2026.sh

set -euo pipefail

OUT_DIR="scenarios/joinmarket/experiments/campaign_2026/definitions"

WALLET_BTC_QUANTILES="0.01,0.1,0.5,2,10,200"
TAKER_BTC_QUANTILES="0.1,0.2,0.5,1.0,2.0,3.0"
ABSOLUTE_FEE_QUANTILES="60,200,700,2000,3000,6000"
RELATIVE_FEE_QUANTILES="0,0.0001,0.0002,0.0005,0.002,0.004"
TUMBLER_DELAYS="0,5,10,15,20,25,30,35"
INJECTED_TAKER_DELAYS=$(seq -s, 0 10 490)

# tumbler: name makercountrange utxos seed...
tumbler() {
  local name=$1 range=$2 utxos=$3; shift 3
  for seed in "$@"; do
    python manager.py genscen-joinmarket \
      --name "${name}_seed${seed}" \
      --seed "$seed" \
      --maker-count 160 \
      --relative-makers 80 \
      --tumbler-taker-count 8 \
      --taker-count 0 \
      --block-count 1000 \
      --tumbler-makercountrange "$range" \
      --tumbler-stage1-timelambda-increase 2 \
      --tumbler-taker-delays "$TUMBLER_DELAYS" \
      --wallet-min-utxos "$utxos" \
      --wallet-max-utxos "$utxos" \
      --use-quantiles \
      --wallet-btc-quantiles "$WALLET_BTC_QUANTILES" \
      --taker-btc-quantiles "$TAKER_BTC_QUANTILES" \
      --fee-absolute-quantiles "$ABSOLUTE_FEE_QUANTILES" \
      --fee-relative-quantiles "$RELATIVE_FEE_QUANTILES" \
      ${EXTRA:-} \
      --out-dir "$OUT_DIR/$name" --force
  done
}

# injected: name makercountrange utxos seed...  (Exp. 3 shape, from final_4_injected_takers.sh)
injected() {
  local name=$1 range=$2 utxos=$3; shift 3
  for seed in "$@"; do
    python manager.py genscen-joinmarket \
      --name "${name}_seed${seed}" \
      --seed "$seed" \
      --maker-count 160 \
      --relative-makers 80 \
      --tumbler-taker-count 8 \
      --taker-count 50 \
      --taker-delays "$INJECTED_TAKER_DELAYS" \
      --taker-max-coinjoins 1 \
      --block-count 550 \
      --tumbler-makercountrange "$range" \
      --tumbler-stage1-timelambda-increase 2 \
      --tumbler-taker-delays "$TUMBLER_DELAYS" \
      --wallet-min-utxos "$utxos" \
      --wallet-max-utxos "$utxos" \
      --use-quantiles \
      --wallet-btc-quantiles "$WALLET_BTC_QUANTILES" \
      --taker-btc-quantiles "$TAKER_BTC_QUANTILES" \
      --fee-absolute-quantiles "$ABSOLUTE_FEE_QUANTILES" \
      --fee-relative-quantiles "$RELATIVE_FEE_QUANTILES" \
      --out-dir "$OUT_DIR/$name" --force
  done
}

# --- needed ------------------------------------------------------------------------------
tumbler  makers_5_1_utxos_10_quantiles_staggered           5,1  10  2 3   # Exp. 1 weak anchor
tumbler  baseline_makers_9_1_utxos_10_quantiles_staggered  9,1  10  2 3   # Exp. 1+2 shared baseline
tumbler  makers_15_1_utxos_10_quantiles_staggered          15,1 10  1 2 3 # recommended setting, no run yet
tumbler  makers_9_1_utxos_3_quantiles_staggered            9,1  3   2 3   # Exp. 2 weak anchor
tumbler  makers_9_1_utxos_25_quantiles_staggered           9,1  25  2 3   # Exp. 2 hard anchor
injected makers_5_1_utxos_3_quantiles_staggered_injected   5,1  3   1 2 3 # Exp. 3, no run yet

# --- recommended -------------------------------------------------------------------------
tumbler  makers_7_1_utxos_10_quantiles_staggered           7,1  10  2 3
# The seed-1 run of this cell is makercountrange [13,2], not the [13,1] the campaign doc
# lists; replicated as [13,2] so the existing run stays reusable as seed 1.
tumbler  makers_13_2_utxos_10_quantiles_staggered          13,2 10  2 3
tumbler  makers_9_1_utxos_5_quantiles_staggered            9,1  5   2 3

# Exp. 4: the baseline with maker fees snapped to the public grid. The snap happens after the
# draw, so at equal seed the market is the baseline's with only the fees quantized (seeds 2
# and 3 pair exactly with baseline seeds 2 and 3). Same client implementation as the baseline;
# offers carry no *_factor, so the plain yield generator runs and announcements are not
# re-randomized -- the "per-announcement randomization off" condition holds as in the baseline.
FEE_ABS_GRID="100,200,500,1000,2000,5000,10000"
FEE_REL_GRID="2e-5,5e-5,1e-4,2e-4,5e-4,1e-3,2e-3,5e-3,1e-2,2e-2,5e-2,1e-1"
EXTRA="--fee-absolute-grid $FEE_ABS_GRID --fee-relative-grid $FEE_REL_GRID" \
tumbler  makers_9_1_utxos_10_quantized_fees                9,1  10  1 2 3

# --- optional (definitions only; run if the trend plot is gappy) -------------------------
tumbler  makers_11_1_utxos_10_quantiles_staggered          11,1 10  2 3
tumbler  makers_9_1_utxos_15_quantiles_staggered           9,1  15  2 3

echo
echo "Definitions written under $OUT_DIR"
