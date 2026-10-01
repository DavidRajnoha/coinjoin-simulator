#!/usr/bin/env python3
"""
Break-glass log collection: pull whatever is still reachable straight from the
pods with kubectl, for when a simulation died before the manager could store
its own logs (OOM kill, lost API client, quota eviction).

Deliberately independent of the manager's own log path -- it talks to kubectl
only, so it still works when the code that normally downloads logs is the thing
that broke. Exposed as `manager-remote collect-logs`; also runnable directly.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Everything the engine creates. btc-node's credentials come from
# containers/btc-node/bitcoin.conf -- keep them in step with it.
POD_PREFIXES = ("jcs-", "btc-node", "irc-", "jm-directory",
                "joinmarket-obwatch", "joinmarket-distributor")
BITCOIN_CLI = "bitcoin-cli -regtest -rpcuser=user -rpcpassword=password"


def run_kubectl(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}", file=sys.stderr)
        return None
    return result.stdout.strip()


def get_pods(namespace):
    cmd = (f"kubectl get pods -n {namespace} --no-headers "
           f"-o custom-columns=NAME:.metadata.name,STATUS:.status.phase")
    output = run_kubectl(cmd)
    if not output:
        return []
    pods = []
    for line in output.split("\n"):
        if line.strip():
            name, status = line.split(None, 1)
            pods.append((name, status))
    return pods


def collect_pod_logs(namespace, pod_name, output_dir):
    print(f"  Collecting logs from {pod_name}...")
    logs = run_kubectl(f"kubectl logs -n {namespace} {pod_name} --tail=10000")
    if logs:
        with open(output_dir / f"{pod_name}.log", "w") as f:
            f.write(logs)
    desc = run_kubectl(f"kubectl describe pod -n {namespace} {pod_name}")
    if desc:
        with open(output_dir / f"{pod_name}_describe.txt", "w") as f:
            f.write(desc)


def get_bitcoin_blocks(namespace, output_dir):
    print("  Collecting Bitcoin blockchain data...")
    blocks_dir = output_dir / "btc-node"
    blocks_dir.mkdir(parents=True, exist_ok=True)

    block_count_str = run_kubectl(
        f"kubectl exec -n {namespace} btc-node -- {BITCOIN_CLI} getblockcount")
    if not block_count_str:
        print("    Could not get block count")
        return
    try:
        block_count = int(block_count_str)
    except ValueError:
        print(f"    Invalid block count: {block_count_str}")
        return

    print(f"    Downloading all {block_count} blocks...")
    for i in range(block_count):
        if i % 100 == 0:
            print(f"    Progress: {i}/{block_count} blocks...")
        block_hash = run_kubectl(
            f"kubectl exec -n {namespace} btc-node -- {BITCOIN_CLI} getblockhash {i}")
        if not block_hash:
            continue
        block_data = run_kubectl(
            f"kubectl exec -n {namespace} btc-node -- {BITCOIN_CLI} getblock {block_hash}")
        if block_data:
            with open(blocks_dir / f"block_{i}.json", "w") as f:
                f.write(block_data)


def collect_crashed(namespace, destination="./emergency_logs", blocks=False):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(destination) / f"{timestamp}_crashed_simulation"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Collecting logs from namespace: {namespace}")
    print(f"Output directory: {output_dir}")

    print("\nFinding simulation pods...")
    pods = get_pods(namespace)
    if not pods:
        print("No pods found!", file=sys.stderr)
        return False

    sim_pods = [p for p in pods if p[0].startswith(POD_PREFIXES)]
    print(f"Found {len(sim_pods)} simulation pods")
    if not sim_pods:
        print("No simulation pods matched; nothing to collect.", file=sys.stderr)
        return False

    logs_dir = output_dir / "pod_logs"
    logs_dir.mkdir(exist_ok=True)
    print("\nCollecting pod logs...")
    for pod_name, _status in sim_pods:
        collect_pod_logs(namespace, pod_name, logs_dir)

    if blocks and any(p[0] == "btc-node" for p in sim_pods):
        print("\nCollecting blockchain data...")
        get_bitcoin_blocks(namespace, output_dir / "data")
    elif not blocks:
        print("\nSkipping blockchain data (pass --blocks to include it)")

    print("\nCollecting orderbook snapshots from workspace...")
    result = subprocess.run(
        f"kubectl exec -n {namespace} deployment/emulation-manager -- "
        f"tar -czf - /workspace/orderbook-snapshots 2>/dev/null",
        shell=True, capture_output=True)
    if result.returncode == 0 and result.stdout:
        with open(output_dir / "orderbook-snapshots.tar.gz", "wb") as f:
            f.write(result.stdout)
        print("  ✓ Saved orderbook snapshots")
    else:
        print("  No orderbook snapshots found in workspace")

    with open(output_dir / "pod_inventory.txt", "w") as f:
        f.write(f"Pods in namespace {namespace} at {timestamp}\n")
        f.write("=" * 80 + "\n\n")
        for pod_name, status in sim_pods:
            f.write(f"{pod_name:<50} {status}\n")

    print(f"\n✓ Log collection complete: {output_dir} ({len(sim_pods)} pods)")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m manager.collect_logs <namespace> [--blocks]")
        print("   or: python manager/remote_cli.py --namespace <ns> collect-logs")
        return 1
    return 0 if collect_crashed(sys.argv[1], blocks="--blocks" in sys.argv) else 1


if __name__ == "__main__":
    sys.exit(main())
