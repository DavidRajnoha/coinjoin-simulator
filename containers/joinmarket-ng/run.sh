#!/bin/sh
# Starts the joinmarket-ng wallet daemon on 28183 (TLS, self-signed).
# Output is teed into the data dir so the log is collected together with
# history.csv / schedules/ at the end of a simulation and still shows in pod logs.
set -eu

DATA_DIR="${JOINMARKET_DATA_DIR:-$HOME/.joinmarket-ng}"
mkdir -p "$DATA_DIR/logs"

exec jmwalletd --data-dir "$DATA_DIR" 2>&1 | tee -a "$DATA_DIR/logs/jmwalletd.log"
