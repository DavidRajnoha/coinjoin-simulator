#!/bin/bash

OUT_DIR="scenarios/joinmarket/experiments/final_4_injected_test"

# Quantile values (rounded to single significant digit where appropriate)
# Wallet BTC amounts (converted from satoshis): 0.009, 0.09, 0.5, 2, 10, 200
WALLET_BTC_QUANTILES="0.01,0.1,0.5,2,10,200"
# Taker BTC amounts (suitable for tumbler operations): 0.1, 0.2, 0.5, 1, 2, 3
TAKER_BTC_QUANTILES="0.1,0.2,0.5,1.0,2.0,3.0"
# Absolute fees (satoshis): 60, 200, 700, 2000, 3000, 6000
ABSOLUTE_FEE_QUANTILES="60,200,700,2000,3000,6000"
# Relative fees: 0, 0.0001, 0.0002, 0.0005, 0.002, 0.004
RELATIVE_FEE_QUANTILES="0,0.0001,0.0002,0.0005,0.002,0.004"

# Stagger 2 tumblers (quarter of 8) starting every 5 blocks to avoid broadcast conflicts
TUMBLER_DELAYS="0,5"

# Generate delays for 12 injected takers (quarter of 50, rounded down) with 10 block increments
# Takers will start at blocks: 0, 10, 20, 30, ..., 110
INJECTED_TAKER_DELAYS=$(seq -s, 0 10 110)

# TEST SCENARIO: UTXO COUNT 3, quarter scale
python manager.py genscen-joinmarket  \
  --name "makers_9_1_utxos_3_quantiles_staggered_injected_test" \
  --maker-count 40 \
  --relative-makers 20 \
  --tumbler-taker-count 2 \
  --taker-count 12 \
  --taker-delays "$INJECTED_TAKER_DELAYS" \
  --taker-max-coinjoins 1 \
  --block-count 150 \
  --tumbler-makercountrange "9,1" \
  --tumbler-stage1-timelambda-increase 2 \
  --tumbler-taker-delays "$TUMBLER_DELAYS" \
  --wallet-min-utxos 3 \
  --wallet-max-utxos 3 \
  --use-quantiles \
  --wallet-btc-quantiles "$WALLET_BTC_QUANTILES" \
  --taker-btc-quantiles "$TAKER_BTC_QUANTILES" \
  --fee-absolute-quantiles "$ABSOLUTE_FEE_QUANTILES" \
  --fee-relative-quantiles "$RELATIVE_FEE_QUANTILES" \
  --out-dir "$OUT_DIR"


echo ""
echo "Generated test scenario with quarter scale in $OUT_DIR"
echo "Makers: 40 (20 absolute, 20 relative)"
echo "Tumbler takers: 2 (delays: $TUMBLER_DELAYS blocks)"
echo "Injected takers: 12 (delays: 0, 10, 20, ..., 110 blocks, each doing 1 coinjoin)"
echo "Blocks: 150"
echo "UTXOs: 3"