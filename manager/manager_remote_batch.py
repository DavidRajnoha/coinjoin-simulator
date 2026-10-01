import sys
import argparse
from kubernetes_local_proxy import KubernetesLocalProxy
from manager_remote import deploy_manager, download_logs_remote

def run_scenario_batch(args):
    """Run a batch of scenarios using the scenario runner"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        runner_id = proxy.start_scenario_runner(
            scenario_dir=args.scenario_dir,
            engine=args.engine,
            image_prefix=args.image_prefix,
            cleanup_wait=args.cleanup_wait
        )

        print(f"✓ Scenario runner started with ID: {runner_id}")
        print(f"  Scenario directory: {args.scenario_dir}")
        print(f"  Engine: {args.engine}")
        print(f"  Cleanup wait: {args.cleanup_wait}s")

        # Save runner ID
        with open(f".runner-{args.namespace}", "w") as f:
            f.write(runner_id)

        print(f"\nUseful commands:")
        print(f"  Check status:  python manager/manager_remote_batch.py --namespace {args.namespace} status")
        print(f"  Follow logs:   python manager/manager_remote_batch.py --namespace {args.namespace} logs -f")
        print(f"  Stop runner:   python manager/manager_remote_batch.py --namespace {args.namespace} stop")

    except Exception as e:
        print(f"Failed to start scenario runner: {e}", file=sys.stderr)
        print(f"Failed to start scenario runner: {e}", file=sys.stderr)
        return False
    return True


def runner_status(args):
    """Check scenario runner status"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get runner ID
        runner_id = args.runner_id
        if not runner_id:
            try:
                with open(f".runner-{args.namespace}", "r") as f:
                    runner_id = f.read().strip()
            except FileNotFoundError:
                print("No runner ID provided and no saved ID found", file=sys.stderr)
                return False

        status = proxy.get_runner_status(runner_id)
        print(f"Runner status: {status['status']}")

        if 'current_status' in status and status['current_status']:
            current = status['current_status']
            print(f"\nCurrent progress:")
            print(f"  Scenario: {current.get('current_scenario', 'Unknown')}")
            print(f"  Completed: {current.get('completed', 0)}/{current.get('total', 0)}")
            print(f"  Last update: {current.get('timestamp', 'Unknown')}")

    except Exception as e:
        print(f"Failed to get runner status: {e}", file=sys.stderr)
        return False
    return True


def runner_logs(args):
    """Get scenario runner logs"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get runner ID
        runner_id = args.runner_id
        if not runner_id:
            try:
                with open(f".runner-{args.namespace}", "r") as f:
                    runner_id = f.read().strip()
            except FileNotFoundError:
                print("No runner ID provided and no saved ID found", file=sys.stderr)
                return False

        # Download logs if --download flag is set
        if args.download:
            success = proxy.download_runner_logs(
                runner_id=runner_id,
                local_destination=args.destination
            )
            if success:
                print("✓ Runner logs downloaded successfully")
            return success
        else:
            # Tail/follow logs
            proxy.tail_runner_logs(
                runner_id=runner_id,
                lines=args.lines,
                follow=args.follow
            )

    except KeyboardInterrupt:
        print("\n✓ Stopped following logs")
    except Exception as e:
        print(f"Failed to get runner logs: {e}", file=sys.stderr)
        return False
    return True


def runner_stop(args):
    """Stop a running scenario batch - terminates entire run"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get runner ID
        runner_id = args.runner_id
        if not runner_id:
            try:
                with open(f".runner-{args.namespace}", "r") as f:
                    runner_id = f.read().strip()
            except FileNotFoundError:
                print("No runner ID provided and no saved ID found", file=sys.stderr)
                return False

        result = proxy.stop_scenario_runner(runner_id)
        print(f"Stop result: {result}")

        if result['status'] == 'stop_signal_sent':
            print("✓ Stop signal sent to scenario runner")
            print("The runner will terminate after the current simulation completes its cleanup")
            print("Environment will be cleaned and ready for a new run")

    except Exception as e:
        print(f"Failed to stop runner: {e}", file=sys.stderr)
        return False
    return True


def runner_skip(args):
    """Skip current scenario and continue to next"""
    try:
        proxy = KubernetesLocalProxy(
            namespace=args.namespace,
            kubectl_context=args.kubectl_context,
            auto_deploy=False
        )

        # Get runner ID
        runner_id = args.runner_id
        if not runner_id:
            try:
                with open(f".runner-{args.namespace}", "r") as f:
                    runner_id = f.read().strip()
            except FileNotFoundError:
                print("No runner ID provided and no saved ID found", file=sys.stderr)
                return False

        result = proxy.skip_scenario_runner(runner_id)
        print(f"Skip result: {result}")

        if result['status'] == 'skip_signal_sent':
            print("✓ Skip signal sent to scenario runner")
            print("The current scenario will be skipped and the runner will continue to the next scenario")

    except Exception as e:
        print(f"Failed to skip scenario: {e}", file=sys.stderr)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Batch Scenario Runner Manager for Kubernetes - Manages running multiple scenarios sequentially",
        prog="manager_remote_batch.py"
    )

    # Global arguments
    parser.add_argument("--namespace", type=str, default="coinjoin",
                       help="Kubernetes namespace where orchestrator is deployed")
    parser.add_argument("--kubectl-context", type=str,
                       help="Kubernetes context to use (optional)")

    subparsers = parser.add_subparsers(dest="command", title="commands",
                                      help="Available commands")

    # Deploy command
    deploy_parser = subparsers.add_parser("deploy",
                                         help="Deploy emulation-manager orchestrator to cluster")
    deploy_parser.add_argument("--image-prefix", type=str, default="",
                              help="Image prefix/registry (e.g., 'myregistry.io/project/')")

    # Download-logs command
    download_parser = subparsers.add_parser("download-logs",
                                           help="Download simulation logs from orchestrator pod")
    download_parser.add_argument("--sim-id", type=str,
                                help="Specific simulation ID (optional)")
    download_parser.add_argument("--all-logs", action="store_true",
                                help="Download all logs from /app/logs directory")
    download_parser.add_argument("-n", "--last-n", type=int, dest="last_n",
                                help="Download the last N simulation log directories (default: 1)")
    download_parser.add_argument("--destination", type=str, default="./logs_download",
                                help="Local directory where logs will be saved")

    # Run command
    batch_parser = subparsers.add_parser("run",
                                        help="Start a new batch scenario runner")
    batch_parser.add_argument("--scenario-dir", type=str, required=True,
                             help="Path to scenario directory inside the orchestrator container (e.g., '/app/scenarios/experiment1')")
    batch_parser.add_argument("--engine", type=str, choices=["joinmarket", "wasabi"],
                             default="joinmarket",
                             help="Coinjoin simulation engine to use")
    batch_parser.add_argument("--image-prefix", type=str, default="",
                             help="Image prefix/registry for simulation pods (e.g., 'drajnoha/')")
    batch_parser.add_argument("--cleanup-wait", type=int, default=90,
                             help="Seconds to wait after cleanup before starting next scenario")

    # Status command
    runner_status_parser = subparsers.add_parser("status",
                                                 help="Check status of running scenario batch")
    runner_status_parser.add_argument("--runner-id", type=str,
                                     help="Runner ID (optional, uses saved ID from .runner-{namespace} if not provided)")

    # Logs command
    runner_logs_parser = subparsers.add_parser("logs",
                                              help="View or download scenario runner output logs")
    runner_logs_parser.add_argument("--runner-id", type=str,
                                    help="Runner ID (optional, uses saved ID from .runner-{namespace} if not provided)")
    runner_logs_parser.add_argument("--lines", type=int, default=50,
                                    help="Number of lines to show from logs (when not downloading)")
    runner_logs_parser.add_argument("--follow", "-f", action="store_true",
                                    help="Follow log output in real-time (like tail -f)")
    runner_logs_parser.add_argument("--download", "-d", action="store_true",
                                    help="Download complete log file instead of viewing")
    runner_logs_parser.add_argument("--destination", type=str, default="./runner_logs",
                                    help="Local directory to save downloaded logs (used with --download)")

    # Stop command
    runner_stop_parser = subparsers.add_parser("stop",
                                              help="Terminate entire scenario batch run (stops current scenario and exits)")
    runner_stop_parser.add_argument("--runner-id", type=str,
                                    help="Runner ID (optional, uses saved ID from .runner-{namespace} if not provided)")

    # Skip command
    runner_skip_parser = subparsers.add_parser("skip",
                                              help="Skip current scenario and continue to next one")
    runner_skip_parser.add_argument("--runner-id", type=str,
                                    help="Runner ID (optional, uses saved ID from .runner-{namespace} if not provided)")

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
        success = run_scenario_batch(args)
    elif args.command == "status":
        success = runner_status(args)
    elif args.command == "logs":
        success = runner_logs(args)
    elif args.command == "stop":
        success = runner_stop(args)
    elif args.command == "skip":
        success = runner_skip(args)
    elif args.command == "download-logs":
        success = download_logs_remote(args)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())