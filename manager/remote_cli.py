#!/usr/bin/env python3
"""
Unified CLI for running simulations on a remote Kubernetes cluster.

One front door for both modes. Whether a run is a single simulation or a batch
is decided by what you point it at, not by which file you remember to invoke:

    run --scenario  X.json   -> one simulation
    run --scenario-dir DIR   -> every scenario in DIR, sequentially

The observability verbs (status / logs / stop / download-logs) work for either,
resolving the active run from .run-<namespace> when no id is given.

This module only routes. The single-simulation implementation still lives in
manager_remote.py and the batch implementation in manager_remote_batch.py, so
the local manager.py interface is untouched and both older CLIs keep working.
"""

import argparse
import os
import sys

if __package__ in (None, ""):
    # Run as `python manager/remote_cli.py`: make the repository root importable.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pylint: disable=wrong-import-position
from manager import manager_remote, manager_remote_batch

# Written by `run`, read by every other verb: "batch" or "single" plus the id,
# so `status` does not need to be told which kind of run is in flight.
RUN_STATE_FILE = ".run-{namespace}"


def _state_path(namespace):
    return RUN_STATE_FILE.format(namespace=namespace)


def _save_run(namespace, kind, run_id):
    with open(_state_path(namespace), "w", encoding="utf-8") as f:
        f.write(f"{kind} {run_id}\n")


def _load_run(namespace):
    """Return (kind, run_id) for the active run, or (None, None).

    Falls back to the dotfiles the two older CLIs wrote, so a run started before
    this CLI existed is still reachable.
    """
    try:
        with open(_state_path(namespace), encoding="utf-8") as f:
            kind, _, run_id = f.read().strip().partition(" ")
            if kind and run_id:
                return kind, run_id
    except FileNotFoundError:
        pass
    for legacy, kind in ((f".runner-{namespace}", "batch"),
                         (f".simulation-{namespace}", "single")):
        try:
            with open(legacy, encoding="utf-8") as f:
                run_id = f.read().strip()
            if run_id:
                return kind, run_id
        except FileNotFoundError:
            continue
    return None, None


def _resolve(args):
    """Work out which run a verb applies to: explicit --id wins, else saved state."""
    if getattr(args, "id", None):
        # An explicit id gives no kind; assume batch only if the caller said so.
        return ("batch" if getattr(args, "batch", False) else "single"), args.id
    kind, run_id = _load_run(args.namespace)
    if not run_id:
        print(f"No active run found for namespace '{args.namespace}'.", file=sys.stderr)
        print("Pass --id, or start one with: run --scenario X.json | --scenario-dir DIR",
              file=sys.stderr)
    return kind, run_id


def cmd_deploy(args):
    return manager_remote.deploy_manager(args)


def cmd_run(args):
    if bool(args.scenario) == bool(args.scenario_dir):
        print("Pass exactly one of --scenario (single run) or --scenario-dir (batch).",
              file=sys.stderr)
        return False

    if args.scenario_dir:
        ok = manager_remote_batch.run_scenario_batch(args)
        if ok:
            kind, run_id = "batch", _load_run_legacy_batch(args.namespace)
            if run_id:
                _save_run(args.namespace, kind, run_id)
        return ok

    ok = manager_remote.run_simulation(args)
    if ok:
        try:
            with open(f".simulation-{args.namespace}", encoding="utf-8") as f:
                _save_run(args.namespace, "single", f.read().strip())
        except FileNotFoundError:
            pass
    return ok


def _load_run_legacy_batch(namespace):
    try:
        with open(f".runner-{namespace}", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def cmd_status(args):
    kind, run_id = _resolve(args)
    if not run_id:
        return False
    print(f"Run {run_id} ({kind})")
    if kind == "batch":
        args.runner_id = run_id
        return manager_remote_batch.runner_status(args)
    args.sim_id = run_id
    return manager_remote.status_remote(args)


def cmd_logs(args):
    kind, run_id = _resolve(args)
    if not run_id:
        return False
    if kind == "batch":
        args.runner_id = run_id
        return manager_remote_batch.runner_logs(args)
    args.sim_id = run_id
    if args.download:
        print("--download applies to batch runs; use download-logs for a single simulation.",
              file=sys.stderr)
        return False
    return manager_remote.get_logs(args)


def cmd_stop(args):
    kind, run_id = _resolve(args)
    if not run_id:
        return False
    if kind == "batch":
        args.runner_id = run_id
        return manager_remote_batch.runner_stop(args)
    args.sim_id = run_id
    return manager_remote.stop_simulation(args)


def cmd_skip(args):
    kind, run_id = _resolve(args)
    if not run_id:
        return False
    if kind != "batch":
        print("skip only applies to a batch run (it advances to the next scenario).",
              file=sys.stderr)
        return False
    args.runner_id = run_id
    return manager_remote_batch.runner_skip(args)


def cmd_download_logs(args):
    return manager_remote.download_logs_remote(args)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="manager-remote",
        description="Run and observe CoinJoin simulations on a remote Kubernetes cluster.",
        epilog=(
            "Examples:\n"
            "  %(prog)s --namespace NS deploy --image-prefix drajnoha/\n"
            "  %(prog)s --namespace NS run --scenario /workspace/scenarios/run.json\n"
            "  %(prog)s --namespace NS run --scenario-dir /workspace/scenarios/batch\n"
            "  %(prog)s --namespace NS status\n"
            "  %(prog)s --namespace NS logs -f\n"
            "  %(prog)s --namespace NS skip          # batch: jump to the next scenario\n"
            "  %(prog)s --namespace NS stop\n"
            "  %(prog)s --namespace NS download-logs -n 3\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--namespace", type=str, default="coinjoin",
                        help="Kubernetes namespace holding the orchestrator (default: coinjoin)")
    parser.add_argument("--kubectl-context", type=str,
                        help="kubectl context to use (default: current context)")

    sub = parser.add_subparsers(dest="command", title="commands")

    p = sub.add_parser("deploy", help="deploy the orchestrator into the namespace")
    p.add_argument("--image-prefix", type=str, default="",
                   help="registry prefix for the manager image, e.g. 'drajnoha/'")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("run", help="start a single simulation or a batch of them")
    src = p.add_argument_group("what to run (pick one)")
    src.add_argument("--scenario", type=str,
                     help="path INSIDE the orchestrator to one scenario json -> single run")
    src.add_argument("--scenario-dir", type=str,
                     help="path INSIDE the orchestrator to a directory of scenario json "
                          "files -> batch run, executed in sorted order")
    p.add_argument("--engine", type=str, choices=["joinmarket", "wasabi"], default="joinmarket",
                   help="simulation engine (default: joinmarket)")
    p.add_argument("--image-prefix", type=str, default="drajnoha/",
                   help="registry prefix for the simulation images (default: drajnoha/)")
    p.add_argument("--cleanup-wait", type=int, default=90,
                   help="batch only: seconds to wait after cleanup between scenarios "
                        "(default: 90)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="show progress of the active run")
    p.add_argument("--id", type=str, help="run id (default: the saved active run)")
    p.add_argument("--batch", action="store_true",
                   help="treat an explicit --id as a batch runner id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", help="view or follow the active run's output")
    p.add_argument("--id", type=str, help="run id (default: the saved active run)")
    p.add_argument("--batch", action="store_true",
                   help="treat an explicit --id as a batch runner id")
    p.add_argument("--lines", type=int, default=50, help="lines to show (default: 50)")
    p.add_argument("--follow", "-f", action="store_true", help="stream new output (tail -f)")
    p.add_argument("--download", "-d", action="store_true",
                   help="batch only: download the full runner log instead of tailing")
    p.add_argument("--destination", type=str, default="./runner_logs",
                   help="where to save with --download (default: ./runner_logs)")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("stop", help="stop the active run (batch: the whole batch)")
    p.add_argument("--id", type=str, help="run id (default: the saved active run)")
    p.add_argument("--batch", action="store_true",
                   help="treat an explicit --id as a batch runner id")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("skip", help="batch only: abandon the current scenario, go to the next")
    p.add_argument("--id", type=str, help="runner id (default: the saved active run)")
    p.set_defaults(batch=True, func=cmd_skip)

    p = sub.add_parser("download-logs", help="download finished simulation log archives")
    p.add_argument("--all-logs", action="store_true", help="download every archive in /app/logs")
    p.add_argument("-n", "--last-n", type=int, dest="last_n",
                   help="download the last N simulation log directories")
    p.add_argument("--destination", type=str, default="./logs_download",
                   help="local directory to save into (default: ./logs_download)")
    p.set_defaults(func=cmd_download_logs)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1
    return 0 if args.func(args) else 1


if __name__ == "__main__":
    sys.exit(main())
