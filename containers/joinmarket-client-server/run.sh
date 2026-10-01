#!/bin/bash
# Starts the RPC server on 28183
# python3 /jm/clientserver/scripts/jmwalletd.py > /home/joinmarket/jmwalletd.log 2>&1
# commented so the logs will show in kubernetes pod logs

set -euo pipefail

MODE=${MODE:-walletd}
# irc       -> talk to the emulator irc-server (reference-only simulations)
# directory -> join the joinmarket-ng directory server over plain TCP (mixed simulations)
JM_MESSAGING=${JM_MESSAGING:-irc}
JM_DIRECTORY_NODES=${JM_DIRECTORY_NODES:-jm-directory:5222}

CFG_DIR=/home/joinmarket/.joinmarket
CFG=$CFG_DIR/joinmarket.cfg

if [ "$JM_MESSAGING" = "directory" ]; then
  sed "s/@@DIRECTORY_NODES@@/$JM_DIRECTORY_NODES/" /home/joinmarket/messaging-onion.cfg > /tmp/messaging.cfg
else
  cp /home/joinmarket/messaging-irc.cfg /tmp/messaging.cfg
fi
cat /home/joinmarket/joinmarket.base.cfg /tmp/messaging.cfg > "$CFG"
echo "joinmarket.cfg assembled with messaging=$JM_MESSAGING"

if [ "$MODE" = "obwatch" ]; then
  # ── Orderbook watcher mode ─────────────────────────────────────────────
 socat TCP-LISTEN:62601,fork,reuseaddr TCP:127.0.0.1:62602 &

  # Launch the orderbook watcher on port 62601 (to the outside, 62602 locally)
  # using a no-blockchain config
  # Note: ob-watcher reads JoinMarket config from the default location.
  exec python3 /jm/clientserver/scripts/obwatch/ob-watcher.py \
       -p 62602 \
       2>&1 | tee -a /home/joinmarket/obwatch.log
else
  # ── Wallet daemon mode (default) ───────────────────────────────────────
  # Forward external 28183 → local-loopback 28182
  # Needed because the jmwallet.d does not allow requests from external connections
  socat TCP-LISTEN:28183,fork,reuseaddr TCP:127.0.0.1:28182 &

  # Launch the wallet daemon bound to 127.0.0.1:28182
  exec python3 /jm/clientserver/scripts/jmwalletd.py --port 28182 \
       2>&1 | tee -a /home/joinmarket/jmwalletd.log
fi
