#!/bin/bash
# Privacy-Enhanced Yield Generator Test Scenarios
# Tests different configurations of privacy-enhanced makers

OUT_DIR="scenarios/joinmarket/experiments/final_6_privacy_enhanced"

echo "Generating privacy-enhanced test scenarios..."

# Test 2: 50% privacy-enhanced, 50% regular makers
echo "Test 2: Mixed 50% privacy-enhanced"
python manager/commands/genscen_joinmarket.py \
  --name "pe_mixed_50pct" \
  --maker-count 100 \
  --relative-makers 40 \
  --tumbler-taker-count 8 \
  --block-count 1000 \
  --tumbler-makercountrange "9,1" \
  --wallet-min-utxos 2 \
  --wallet-max-utxos 2 \
  --privacy-enhanced-percentage 0.5 \
  --force \
  --out-dir "$OUT_DIR"