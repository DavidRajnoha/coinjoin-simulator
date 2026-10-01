#!/usr/bin/env python3
"""
JoinMarket Scenario Runner
Runs all scenarios in a given folder with proper cleanup and error handling
"""

import os
import subprocess
import time
import json
import argparse
import signal
import threading
import psutil
from datetime import datetime
from typing import List, Tuple, Optional


class ScenarioRunner:
    def __init__(self,
                 scenario_dir: str,
                 namespace: str = "rajnoha-ns",
                 image_prefix: str = "drajnoha/",
                 proxy: str = "socks5://127.0.0.1:8123",
                 shadowsocks_config: str = None,
                 cleanup_wait: int = 150,
                 in_cluster: bool = False):

        self.scenario_dir = scenario_dir
        self.namespace = namespace
        self.image_prefix = image_prefix
        self.proxy = proxy
        self.shadowsocks_config = shadowsocks_config
        self.cleanup_wait = cleanup_wait
        self.sslocal_process = None
        self.results = []
        self.in_cluster = in_cluster
        self.current_scenario_file = None
        self.stop_requested = False
        self.skip_requested = False
        self.current_process = None  # Track currently running subprocess
        self.memory_monitor_stop = threading.Event()
        self.memory_monitor_thread = None

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_stop_signal)   # Terminate entire run
        signal.signal(signal.SIGINT, self._handle_stop_signal)    # Ctrl+C = stop
        signal.signal(signal.SIGUSR1, self._handle_skip_signal)   # Skip to next scenario


    def _handle_stop_signal(self, signum, frame):
        """Handle stop signals (SIGTERM/SIGINT) - terminate entire run"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n[{self.get_timestamp()}] Received {signal_name}, terminating entire run...")
        self.stop_requested = True

        # Forward the signal to the currently running subprocess (manager.py)
        # This allows manager.py to do its cleanup (stop_coinjoins, store_logs, etc.)
        if self.current_process and self.current_process.poll() is None:  # Process is still running
            print(f"[{self.get_timestamp()}] Forwarding {signal_name} to running manager.py (PID: {self.current_process.pid})...")
            try:
                self.current_process.send_signal(signum)
            except ProcessLookupError:
                # Process already terminated
                pass

    def _handle_skip_signal(self, signum, frame):
        """Handle skip signal (SIGUSR1) - skip current scenario and continue to next"""
        print(f"\n[{self.get_timestamp()}] Received SIGUSR1, skipping current scenario...")
        self.skip_requested = True

        # Forward SIGTERM to the currently running subprocess to stop it gracefully
        if self.current_process and self.current_process.poll() is None:  # Process is still running
            print(f"[{self.get_timestamp()}] Sending SIGTERM to running manager.py (PID: {self.current_process.pid})...")
            try:
                self.current_process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                # Process already terminated
                pass

    def _monitor_memory(self):
        """Background thread to monitor memory usage every 30 seconds"""
        process = psutil.Process()
        memory_log_file = "/workspace/memory_usage.log"

        while not self.memory_monitor_stop.is_set():
            try:
                mem_info = process.memory_info()
                mem_mb = mem_info.rss / (1024 * 1024)  # RSS in MB
                mem_percent = process.memory_percent()

                # Get system-wide memory info
                sys_mem = psutil.virtual_memory()
                sys_mem_used_mb = sys_mem.used / (1024 * 1024)
                sys_mem_total_mb = sys_mem.total / (1024 * 1024)

                log_msg = f"[MEMORY] {self.get_timestamp()} Process: {mem_mb:.1f} MB ({mem_percent:.1f}%), System: {sys_mem_used_mb:.0f}/{sys_mem_total_mb:.0f} MB ({sys_mem.percent:.1f}%)"
                print(log_msg)

                # Also write to persistent file to survive crashes
                try:
                    with open(memory_log_file, "a") as f:
                        f.write(log_msg + "\n")
                        f.flush()
                except Exception as write_err:
                    print(f"[MEMORY] Error writing to log file: {write_err}")

            except Exception as e:
                print(f"[MEMORY] Error getting memory stats: {e}")

            # Wait 30 seconds or until stop is signaled
            self.memory_monitor_stop.wait(30)

    def _start_memory_monitor(self):
        """Start the memory monitoring thread"""
        if self.memory_monitor_thread is None or not self.memory_monitor_thread.is_alive():
            self.memory_monitor_stop.clear()
            self.memory_monitor_thread = threading.Thread(target=self._monitor_memory, daemon=True)
            self.memory_monitor_thread.start()
            print("[MEMORY] Memory monitoring started (logging every 30 seconds)")

    def _stop_memory_monitor(self):
        """Stop the memory monitoring thread"""
        if self.memory_monitor_thread and self.memory_monitor_thread.is_alive():
            self.memory_monitor_stop.set()
            self.memory_monitor_thread.join(timeout=2)
            print("[MEMORY] Memory monitoring stopped")

    def _write_current_status(self):
        """Write current status to a file for external monitoring"""
        if self.in_cluster:
            status = {
                "current_scenario": self.current_scenario_file,
                "timestamp": self.get_timestamp(),
                "completed": len([r for r in self.results if r.get("success", False)]),
                "total": len(self.results),
                "stop_requested": self.stop_requested,
                "skip_requested": self.skip_requested
            }
            # Write to a known location in the container
            with open("/tmp/scenario-runner-status.json", "w") as f:
                json.dump(status, f)

    def cleanup_kubernetes(self) -> bool:
        """Clean up kubernetes resources"""
        print(f"[{self.get_timestamp()}] Cleaning up kubernetes resources...")

        cmd = [
            "python", "manager.py",
            "--driver", "kubernetes",
            "--engine", "joinmarket",
            "clean",
            "--reuse-namespace",
            "--namespace", self.namespace,
            "--image-prefix", self.image_prefix
        ]

        if self.in_cluster:
            cmd.insert(4, "--in-cluster")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                print(f"[{self.get_timestamp()}] Cleanup successful, waiting {self.cleanup_wait} seconds...")
                time.sleep(self.cleanup_wait)
                return True
            else:
                print(f"[{self.get_timestamp()}] ERROR: Cleanup failed")
                print(f"STDOUT: {result.stdout}")
                print(f"STDERR: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            print(f"[{self.get_timestamp()}] ERROR: Cleanup timed out after 5 minutes")
            return False
        except Exception as e:
            print(f"[{self.get_timestamp()}] ERROR during cleanup: {e}")
            return False

    def run_scenario(self, scenario_file: str) -> Tuple[bool, float]:
        """Run a single scenario"""
        print(f"\n[{self.get_timestamp()}] Running scenario: {scenario_file}")
        self.current_scenario_file = scenario_file
        self._write_current_status()


        cmd = [
            "python", "manager.py",
            "--driver", "kubernetes",
            "--engine", "joinmarket",
            "run",
            "--namespace", self.namespace,
            "--reuse-namespace",
            "--image-prefix", self.image_prefix,
            "--scenario", scenario_file
        ]

        if self.in_cluster:
            cmd.insert(4, "--in-cluster")

        if not self.in_cluster and self.proxy:
            cmd.extend(["--proxy", self.proxy])

        start_time = time.time()

        try:
            # Run the scenario with non-buffering output
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            self.current_process = process  # Track the current process

            # Helper function to stream output from a pipe
            def stream_output(pipe, prefix):
                try:
                    for line in iter(pipe.readline, ''):
                        if line:
                            print(f"{prefix}{line.rstrip()}", flush=True)
                except Exception as e:
                    print(f"[ERROR] Stream exception: {e}", flush=True)
                finally:
                    pipe.close()

            # Create threads to stream both stdout and stderr in real-time
            # This prevents memory accumulation in subprocess.PIPE buffers
            stdout_thread = threading.Thread(target=stream_output, args=(process.stdout, "  "), daemon=True)
            stderr_thread = threading.Thread(target=stream_output, args=(process.stderr, "  [ERR] "), daemon=True)

            stdout_thread.start()
            stderr_thread.start()

            # Wait for process completion
            return_code = process.wait()
            duration = time.time() - start_time

            # Wait for output threads to finish (with timeout to prevent hanging)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            self.current_process = None  # Clear when done

            if return_code == 0:
                print(f"[{self.get_timestamp()}] SUCCESS: Scenario completed in {duration:.1f} seconds")
                return True, duration
            else:
                print(f"[{self.get_timestamp()}] ERROR: Scenario failed after {duration:.1f} seconds (exit code: {return_code})")
                return False, duration

        except Exception as e:
            duration = time.time() - start_time
            print(f"[{self.get_timestamp()}] ERROR running scenario: {e}")
            return False, duration

    def get_scenarios(self) -> List[str]:
        """Get all JSON scenario files in the directory"""
        scenarios = []

        for root, dirs, files in os.walk(self.scenario_dir):
            for file in files:
                if file.endswith('.json'):
                    scenarios.append(os.path.join(root, file))

        return sorted(scenarios)

    def get_timestamp(self) -> str:
        """Get current timestamp for logging"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_results(self):
        """Save run results to file"""
        results_file = os.path.join('logs', f"run_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

        with open(results_file, 'w') as f:
            json.dump(self.results, f, indent=2)

        print(f"\nResults saved to: {results_file}")

    def run_all(self, start_from: Optional[str] = None):
        """Run all scenarios with proper error handling"""
        scenarios = self.get_scenarios()

        if not scenarios:
            print(f"No scenarios found in {self.scenario_dir}")
            return

        print(f"Found {len(scenarios)} scenarios to run")

        # Skip scenarios if start_from is specified
        start_index = 0
        if start_from:
            for i, scenario in enumerate(scenarios):
                if start_from in scenario:
                    start_index = i
                    print(f"Starting from scenario: {scenario}")
                    break

        try:
            # Start memory monitoring
            self._start_memory_monitor()

            # Initial cleanup
            print("\nPerforming initial cleanup...")
            self.cleanup_kubernetes()

            # Run each scenario
            for i, scenario in enumerate(scenarios[start_index:], start=start_index):
                # Check if stop was requested (terminate entire run)
                if self.stop_requested:
                    print(f"\n[{self.get_timestamp()}] Stop requested, terminating run after cleanup...")
                    break

                print(f"\n{'=' * 80}")
                print(f"Scenario {i + 1}/{len(scenarios)}: {os.path.basename(scenario)}")
                print(f"{'=' * 80}")

                # Try to run the scenario
                success, duration = self.run_scenario(scenario)

                # Check if skip was requested during the run
                if self.skip_requested:
                    print(f"[{self.get_timestamp()}] Skip requested, marking scenario as skipped and continuing to next...")
                    self.results.append({
                        "scenario": scenario,
                        "success": False,
                        "skipped": True,
                        "duration": duration,
                        "timestamp": self.get_timestamp()
                    })
                    self.skip_requested = False  # Reset for next scenario
                else:
                    self.results.append({
                        "scenario": scenario,
                        "success": success,
                        "duration": duration,
                        "timestamp": self.get_timestamp()
                    })

                    # If failed and not skipped, try cleanup and retry once (but not if stop requested)
                    if not success and not self.stop_requested:
                        print(f"\n[{self.get_timestamp()}] Scenario failed, attempting cleanup and retry...")

                        if self.cleanup_kubernetes():
                            print(f"[{self.get_timestamp()}] Retrying scenario...")
                            success, duration = self.run_scenario(scenario)

                            self.results[-1]["retry"] = True
                            self.results[-1]["retry_success"] = success
                            self.results[-1]["retry_duration"] = duration

                            if not success:
                                print(f"[{self.get_timestamp()}] Scenario failed on retry, continuing to next...")
                        else:
                            print(f"[{self.get_timestamp()}] Cleanup failed, skipping retry")

                # Clean up after each scenario (unless stop requested)
                if not self.stop_requested and i < len(scenarios) - 1:  # Don't cleanup after last scenario
                    print(f"\n[{self.get_timestamp()}] Cleaning up before next scenario...")
                    self.cleanup_kubernetes()

                # Save intermediate results
                self.save_results()

        except KeyboardInterrupt:
            print(f"\n[{self.get_timestamp()}] Interrupted by user (Ctrl+C)")
            self.stop_requested = True
        finally:
            # Stop memory monitoring
            self._stop_memory_monitor()

            # Final cleanup
            print(f"\n[{self.get_timestamp()}] Performing final cleanup...")
            self.cleanup_kubernetes()

            # Print summary
            print(f"\n{'=' * 80}")
            print("RUN SUMMARY")
            print(f"{'=' * 80}")

            successful = sum(1 for r in self.results if r["success"] or r.get("retry_success", False))
            failed = len(self.results) - successful

            print(f"Total scenarios: {len(self.results)}")
            print(f"Successful: {successful}")
            print(f"Failed: {failed}")

            if failed > 0:
                print("\nFailed scenarios:")
                for r in self.results:
                    if not r["success"] and not r.get("retry_success", False):
                        print(f"  - {os.path.basename(r['scenario'])}")

            # Save final results
            self.save_results()


def main():
    parser = argparse.ArgumentParser(description="Run JoinMarket scenarios")
    parser.add_argument("--scenario_dir", help="Directory containing scenario JSON files")
    parser.add_argument("--namespace", default="rajnoha-ns", help="Kubernetes namespace")
    parser.add_argument("--in-cluster", action="store_true", help="When scenario runner is running in cluster")
    parser.add_argument("--image-prefix", default="drajnoha/", help="Docker image prefix")
    parser.add_argument("--proxy", default="socks5://127.0.0.1:8123", help="Proxy URL")
    parser.add_argument("--shadowsocks-config",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "shadowsocks", "config_local.yaml"),
                        help="Shadowsocks config file")
    parser.add_argument("--cleanup-wait", type=int, default=90,
                        help="Seconds to wait after cleanup")
    parser.add_argument("--start-from", help="Start from scenario containing this string")

    args = parser.parse_args()

    runner = ScenarioRunner(
        scenario_dir=args.scenario_dir,
        namespace=args.namespace,
        image_prefix=args.image_prefix,
        proxy=args.proxy,
        shadowsocks_config=args.shadowsocks_config,
        cleanup_wait=args.cleanup_wait,
        in_cluster=args.in_cluster
    )

    runner.run_all(start_from=args.start_from)


if __name__ == "__main__":
    main()