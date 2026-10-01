#!/usr/bin/env python3
"""
Remote Simulation Manager - CLI interface for orchestrator commands
"""

import sys
import argparse
from kubernetes_local_proxy import KubernetesLocalProxy


def deploy_manager(args):
    """Deploy the simulation manager to the remote cluster"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=True,
            image_prefix=args.image_prefix
        )
        print(f"✓ Simulation manager deployed to namespace '{args.namespace}'")
    except Exception as e:
        print(f"Failed to deploy manager: {e}", file=sys.stderr)
        return False
    return True


def run_simulation(args):
    """Start a simulation without waiting for completion"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False  # Assume orchestrator is already deployed
        )

        # Start the simulation - this returns immediately
        simulation_id = proxy.start_simulation(
            scenario_path=args.scenario,
            engine=args.engine,
            image_prefix=args.image_prefix
        )

        print(f"✓ Simulation started with ID: {simulation_id}")
        print(f"  Scenario: {args.scenario}")
        print(f"  Engine: {args.engine}")

        # Save the simulation ID to a file for easy reference
        with open(f".simulation-{args.namespace}", "w") as f:
            f.write(simulation_id)

        print(f"\nTo check status: manager-remote --namespace {args.namespace} status --sim-id {simulation_id}")

    except Exception as e:
        print(f"Failed to start simulation: {e}", file=sys.stderr)
        return False
    return True


def stop_simulation(args):
    """Stop a running simulation"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get simulation ID from args or saved file
        sim_id = args.sim_id
        if not sim_id:
            try:
                with open(f".simulation-{args.namespace}", "r") as f:
                    sim_id = f.read().strip()
                print(f"Using saved simulation ID: {sim_id}")
            except FileNotFoundError:
                print("No simulation ID provided and no saved ID found", file=sys.stderr)
                print("Use --sim-id or run a simulation first")
                return False

        # Stop the simulation
        result = proxy.stop_simulation(simulation_id=sim_id)
        print(f"✓ Stop command sent to simulation {sim_id}")
        print(f"  Result: {result}")

    except Exception as e:
        print(f"Failed to stop simulation: {e}", file=sys.stderr)
        return False
    return True


def status_remote(args):
    """Check orchestrator or simulation status"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # If simulation ID provided, check simulation status
        if hasattr(args, 'sim_id') and args.sim_id:
            sim_id = args.sim_id
        else:
            # Try to read saved simulation ID
            try:
                with open(f".simulation-{args.namespace}", "r") as f:
                    sim_id = f.read().strip()
            except FileNotFoundError:
                sim_id = None

        if sim_id:
            print(f"Checking status of simulation {sim_id}...")
            status = proxy.get_status(simulation_id=sim_id)
            print(f"Status: {status['status']}")
            if 'pid' in status:
                print(f"  Process ID: {status['pid']}")
            if 'error' in status:
                print(f"  Error: {status['error']}")
            if 'warning' in status:
                print(f"  Warning: {status['warning']}")
        else:
            # Just check if orchestrator is accessible
            print(f"✓ Orchestrator is accessible in namespace '{args.namespace}'")

    except Exception as e:
        print(f"✗ Status check failed: {e}", file=sys.stderr)
        return False
    return True


def get_logs(args):
    """Get logs from a simulation"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get simulation ID
        sim_id = args.sim_id
        if not sim_id:
            try:
                with open(f".simulation-{args.namespace}", "r") as f:
                    sim_id = f.read().strip()
                print(f"Using saved simulation ID: {sim_id}")
            except FileNotFoundError:
                print("No simulation ID provided and no saved ID found", file=sys.stderr)
                return False

        # Show logs
        if args.follow:
            print(f"Following logs for simulation {sim_id} (Ctrl+C to stop)...")
            proxy.tail_logs(
                simulation_id=sim_id,
                lines=args.lines,
                follow=True
            )
        else:
            print(f"Last {args.lines} lines from simulation {sim_id}:")
            print("-" * 80)
            proxy.tail_logs(
                simulation_id=sim_id,
                lines=args.lines,
                follow=False
            )

    except KeyboardInterrupt:
        print("\n✓ Stopped following logs")
    except Exception as e:
        print(f"Failed to get logs: {e}", file=sys.stderr)
        return False
    return True


def download_logs_remote(args):
    """Download simulation logs from the orchestrator"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get last_n parameter if provided
        last_n = getattr(args, 'last_n', None)

        success = proxy.download_logs(args.destination, args.all_logs, last_n=last_n)

        if success:
            print(f"\n✓ Logs downloaded successfully to {args.destination}")
            print(f"You can now analyze the logs locally")
        else:
            print("\n✗ Failed to download logs")

    except Exception as e:
        print(f"Error downloading logs: {e}", file=sys.stderr)
        return False
    return success




def main():
    parser = argparse.ArgumentParser(
        description="Remote Simulation Manager for Kubernetes",
        prog="manager-remote"
    )

    # Global arguments
    parser.add_argument("--namespace", type=str, default="coinjoin", help="Kubernetes namespace")
    parser.add_argument("--kubectl-context", type=str, help="Kubernetes context to use")

    subparsers = parser.add_subparsers(dest="command", title="commands", help="Available commands")

    # Deploy command (existing)
    deploy_parser = subparsers.add_parser("deploy", help="Deploy simulation manager to cluster")
    deploy_parser.add_argument("--image-prefix", type=str, default="", help="Image prefix/registry")

    # Run command
    run_parser = subparsers.add_parser("run", help="Start a simulation")
    run_parser.add_argument("--scenario", type=str, required=True,
                            help="Scenario path in container (e.g., scenarios/joinmarket/test/tumbler_1_maker_3.json)")
    run_parser.add_argument("--engine", type=str, choices=["joinmarket", "wasabi"],
                            default="joinmarket", help="Simulation engine to use")
    run_parser.add_argument("--image-prefix", type=str, default="", help="Image prefix for simulation images")

    # Status command
    status_parser = subparsers.add_parser("status", help="Check orchestrator or simulation status")
    status_parser.add_argument("--sim-id", type=str, help="Specific simulation ID to check")

    # Stop command
    stop_parser = subparsers.add_parser("stop", help="Stop a running simulation")
    stop_parser.add_argument("--sim-id", type=str, help="Simulation ID to stop")

    # Logs command
    logs_parser = subparsers.add_parser("logs", help="Get simulation logs")
    logs_parser.add_argument("--sim-id", type=str, help="Simulation ID")
    logs_parser.add_argument("--lines", type=int, default=50, help="Number of lines to show")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output (like tail -f)")

    # Download-logs command
    download_parser = subparsers.add_parser("download-logs", help="Download simulation logs")
    download_parser.add_argument("--sim-id", type=str, help="Simulation ID")
    download_parser.add_argument("--all-logs", action="store_true",
                                 help="Download all logs from the orchestrator")
    download_parser.add_argument("-n", "--last-n", type=int, dest="last_n",
                                 help="Download the last N simulation log directories (default: 1)")
    download_parser.add_argument("--destination", type=str, default="./logs_download",
                                 help="Local directory to save logs")


    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    print("note: manager/remote_cli.py is the unified entrypoint and handles both "
          "single and batch runs; this CLI still works.", file=sys.stderr)

    # Route to appropriate function
    if args.command == "deploy":
        success = deploy_manager(args)
    elif args.command == "run":
        success = run_simulation(args)
    elif args.command == "status":
        success = status_remote(args)
    elif args.command == "stop":
        success = stop_simulation(args)
    elif args.command == "logs":
        success = get_logs(args)
    elif args.command == "download-logs":
        success = download_logs_remote(args)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())