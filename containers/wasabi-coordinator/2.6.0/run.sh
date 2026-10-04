#!/bin/bash
set -eu

export WASABI_BIND="http://0.0.0.0:37128"

echo "Wasabi binding to $WASABI_BIND"
rm -rf /home/wasabi/.walletwasabi
mkdir -p /home/wasabi/.walletwasabi/coordinator

config_deadline=$((SECONDS + ${WASABI_COORDINATOR_CONFIG_TIMEOUT:-120}))
while [ ! -f /home/wasabi/coordinator-config.ready ]; do
    if [ "$SECONDS" -ge "$config_deadline" ]; then
        echo "Timed out waiting for coordinator configuration upload" >&2
        exit 1
    fi
    sleep 1
done

mv /home/wasabi/coordinator-config.json /home/wasabi/.walletwasabi/coordinator/Config.json
rm /home/wasabi/coordinator-config.ready
exec ./WalletWasabi.Coordinator --loglevel=trace
