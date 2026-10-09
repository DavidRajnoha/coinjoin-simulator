import json
import os
import shlex
import subprocess
import time
import uuid

import backoff
import yaml

from manager.exceptions import CoinjoinEmulatorError

# File transfer settings
CHUNK_SIZE_MB = 10
LARGE_FILE_THRESHOLD_MB = 20
MAX_DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds


class KubernetesLocalProxy:
    """
    Local driver that delegates operations to a remote orchestrator pod in Kubernetes.
    This allows local management while executing operations inside the cluster.
    """

    def __init__(self, namespace="coinjoin", orchestrator_pod=None, kubectl_context=None, auto_deploy=True, image_prefix=""):
        self.namespace = namespace
        self.orchestrator_pod = orchestrator_pod or "deployment/emulation-manager"
        self.kubectl_context = kubectl_context
        self.simulation_id = str(uuid.uuid4())[:8]
        self._kubectl_base_cmd = self._build_kubectl_cmd()

        if auto_deploy and not self.deploy_manager(image_prefix=image_prefix):
            raise CoinjoinEmulatorError("Could not deploy the orchestrator")

        # Test connection to orchestrator
        self._test_connection()

    def _build_kubectl_cmd(self):
        """Build base kubectl command with context if provided"""
        cmd = ["kubectl"]
        if self.kubectl_context:
            cmd.extend(["--context", self.kubectl_context])
        return cmd

    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    def _test_connection(self):
        """Test connection to the orchestrator pod"""
        try:
            result = self._kubectl_exec(["echo", "connection_test"])
            if "connection_test" not in result:
                raise Exception("Unexpected response from orchestrator")
            print(f"✓ Connected to orchestrator in namespace {self.namespace}")
        except Exception as e:
            raise Exception(f"Failed to connect to orchestrator: {e}")

    def _kubectl_exec(self, cmd, input_data=None, capture_output=True):
        """Execute command in the orchestrator pod via kubectl exec"""
        full_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--"
        ] + cmd

        try:
            if input_data:
                result = subprocess.run(
                    full_cmd,
                    input=input_data,
                    text=True,
                    capture_output=capture_output,
                    check=True
                )
            else:
                result = subprocess.run(
                    full_cmd,
                    text=True,
                    capture_output=capture_output,
                    check=True
                )
            return result.stdout if capture_output else None
        except subprocess.CalledProcessError as e:
            print(f"Command failed: {' '.join(full_cmd)}")
            print(f"Error: {e.stderr if hasattr(e, 'stderr') else str(e)}")
            raise


    def _orchestrator_cmd(self, manager_args, input_data=None):
        """Execute manager.py command in orchestrator with arguments"""
        cmd = [
                  "python", "manager.py",
                  "--driver", "kubernetes", "--in-cluster",
                  "--namespace", self.namespace, "--reuse-namespace"
              ] + manager_args

        return self._kubectl_exec(cmd, input_data)

    def start_scenario_runner(self, scenario_dir, engine="joinmarket", image_prefix="", cleanup_wait=90):
        """Start the scenario runner inside the orchestrator pod"""
        runner_id = str(uuid.uuid4())[:8]
        print(f"Starting scenario runner {runner_id} in orchestrator...")

        # The scenario directory should be a path inside the container
        runner_cmd = [
            "python", "scenario_runner.py",
            "--scenario_dir", scenario_dir,
            "--namespace", self.namespace,
            "--image-prefix", image_prefix,
            "--cleanup-wait", str(cleanup_wait),
            "--engine", engine,
            "--in-cluster"  # New flag we'll add
        ]

        # Create a dedicated directory for this runner
        background_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            RUNNER_DIR=/tmp/scenario-runners/{runner_id}
            mkdir -p $RUNNER_DIR

            # Start the runner in background
            nohup {' '.join(runner_cmd)} > $RUNNER_DIR/output.log 2>&1 &
            RUNNER_PID=$!

            # Save runner metadata
            echo $RUNNER_PID > $RUNNER_DIR/pid
            echo '{{"status": "running", "runner_id": "{runner_id}", "scenario_dir": "{scenario_dir}", "start_time": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}' > $RUNNER_DIR/status.json

            echo "Started scenario runner with PID: $RUNNER_PID"
            """
        ]

        subprocess.run(background_cmd, check=True)
        print(f"✓ Scenario runner {runner_id} started")
        return runner_id



    def start_simulation(self, scenario_path, engine="joinmarket", image_prefix=""):
        """Start simulation in orchestrator"""
        print(f"Starting simulation {self.simulation_id} in orchestrator...")

        manager_cmd = [
            "python", "manager.py",
            "--driver", "kubernetes",
            "--in-cluster",
            "--engine", engine,
            "run",
            "--namespace", self.namespace,
            "--reuse-namespace",
            "--image-prefix", image_prefix,
            "--scenario", scenario_path  # This is the path INSIDE the container
        ]
        # get_status() reads exit_status to tell a finished run from a failed
        # one, so the manager's exit code has to be recorded when it ends.
        completion_wrapper = shlex.quote(
            f'{shlex.join(manager_cmd)}; code=$?; echo "$code" > "$1/exit_status"; exit "$code"'
        )

        background_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
                SIM_DIR=/tmp/simulations/{self.simulation_id}
                mkdir -p $SIM_DIR
                nohup sh -c {completion_wrapper} \
                    sh "$SIM_DIR" > "$SIM_DIR/output.log" 2>&1 &
                echo $! > $SIM_DIR/pid
                echo '{{"status": "running", "pid": "'$!'", "start_time": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'", "scenario": "{scenario_path}"}}' > $SIM_DIR/status.json
            """
        ]
        print(f"Running command: {' '.join(background_cmd)}")

        subprocess.run(background_cmd, check=True)
        print(f"✓ Simulation {self.simulation_id} started in background")

        return self.simulation_id

    def get_status(self, simulation_id=None):
        """Get detailed status of a simulation"""
        sim_id = simulation_id or self.simulation_id

        # Check if the simulation directory exists and get process status
        status_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            SIM_DIR=/tmp/simulations/{sim_id}

            # Check if simulation directory exists
            if [ ! -d "$SIM_DIR" ]; then
                echo '{{"status": "not_found", "simulation_id": "{sim_id}"}}'
                exit 0
            fi

            # Read the PID and check if process is running
            if [ -f "$SIM_DIR/pid" ]; then
                PID=$(cat "$SIM_DIR/pid")
                if ps -p $PID > /dev/null 2>&1; then
                    # Process is running - update status
                    echo '{{"status": "running", "pid": "'$PID'", "simulation_id": "{sim_id}"}}'
                else
                    # Process finished - check exit status
                    if [ -f "$SIM_DIR/exit_status" ]; then
                        EXIT_CODE=$(cat "$SIM_DIR/exit_status")
                        if [ "$EXIT_CODE" = "0" ]; then
                            echo '{{"status": "completed", "exit_code": 0, "simulation_id": "{sim_id}"}}'
                        else
                            echo '{{"status": "failed", "exit_code": '$EXIT_CODE', "simulation_id": "{sim_id}"}}'
                        fi
                    else
                        # Process ended but no exit status recorded
                        echo '{{"status": "terminated", "simulation_id": "{sim_id}"}}'
                    fi
                fi
            else
                echo '{{"status": "error", "message": "No PID file found", "simulation_id": "{sim_id}"}}'
            fi
            """
        ]

        try:
            result = subprocess.run(status_cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.strip())
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            return {"status": "error", "message": str(e), "simulation_id": sim_id}


    def tail_logs(self, lines=50, follow=False, simulation_id=None):
        """
        Tail simulation logs.

        Args:
            lines: Number of lines to show
            follow: If True, keep streaming new lines (like tail -f)
            simulation_id: Which simulation to tail (defaults to current)
        """
        sim_id = simulation_id or self.simulation_id

        if follow:
            # This will keep the connection open and stream logs
            tail_cmd = [
                "tail", "-f", f"/tmp/simulations/{sim_id}/output.log"
            ]

            # Use stream for real-time output
            print(f"Streaming logs for simulation {sim_id} (Ctrl+C to stop)...")
            try:
                # This is a blocking call that streams output
                subprocess.run(
                    self._kubectl_base_cmd + ["exec", "-n", self.namespace,
                                              self.orchestrator_pod, "--"] + tail_cmd,
                    check=True
                )
            except KeyboardInterrupt:
                print("\nStopped streaming logs")
        else:
            # Just get the last N lines
            tail_cmd = self._kubectl_base_cmd + [
                "exec", "-n", self.namespace,
                self.orchestrator_pod, "--",
                "tail", f"-{lines}", f"/tmp/simulations/{sim_id}/output.log"
            ]

            result = subprocess.run(tail_cmd, capture_output=True, text=True)
            print(result.stdout)

    def get_runner_status(self, runner_id=None):
        """
        Get status of a scenario runner, including current scenario progress.
        """
        r_id = runner_id or getattr(self, 'runner_id', None)
        if not r_id:
            return {"status": "error", "message": "No runner ID provided"}

        status_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            RUNNER_DIR=/tmp/scenario-runners/{r_id}

            if [ ! -d "$RUNNER_DIR" ]; then
                echo '{{"status": "not_found", "runner_id": "{r_id}"}}'
                exit 0
            fi

            # Check if process is running
            if [ -f "$RUNNER_DIR/pid" ]; then
                PID=$(cat "$RUNNER_DIR/pid")
                if ps -p $PID > /dev/null 2>&1; then
                    # Runner is active - get current scenario status
                    CURRENT_STATUS="{'{}'}"
                    if [ -f "/tmp/scenario-runner-status.json" ]; then
                        CURRENT_STATUS=$(cat /tmp/scenario-runner-status.json)
                    fi
                    echo '{{"status": "running", "pid": "'$PID'", "runner_id": "{r_id}", "current_status": '$CURRENT_STATUS'}}'
                else
                    # Runner finished
                    echo '{{"status": "completed", "runner_id": "{r_id}"}}'
                fi
            else
                echo '{{"status": "error", "message": "No PID file", "runner_id": "{r_id}"}}'
            fi
            """
        ]

        try:
            result = subprocess.run(status_cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.strip())
        except Exception as e:
            return {"status": "error", "message": str(e), "runner_id": r_id}

    def tail_runner_logs(self, lines=50, follow=False, runner_id=None):
        """
        Tail scenario runner logs.

        This shows the output from the scenario runner, which includes all simulation outputs.
        """
        r_id = runner_id or getattr(self, 'runner_id', None)
        if not r_id:
            print("No runner ID provided")
            return

        log_file = f"/tmp/scenario-runners/{r_id}/output.log"

        if follow:
            # Stream logs in real-time
            tail_cmd = ["tail", "-f", log_file]
            print(f"Streaming logs for scenario runner {r_id} (Ctrl+C to stop)...")

            try:
                subprocess.run(
                    self._kubectl_base_cmd + ["exec", "-n", self.namespace,
                                              self.orchestrator_pod, "--"] + tail_cmd,
                    check=True
                )
            except KeyboardInterrupt:
                print("\nStopped streaming logs")
        else:
            # Just show last N lines
            tail_cmd = self._kubectl_base_cmd + [
                "exec", "-n", self.namespace,
                self.orchestrator_pod, "--",
                "tail", f"-{lines}", log_file
            ]

            result = subprocess.run(tail_cmd, capture_output=True, text=True)
            print(result.stdout)

    def _resolve_pod_name(self):
        """
        Resolve deployment/statefulset references to actual pod name.

        kubectl exec works with 'deployment/name' but kubectl cp requires actual pod names.
        This resolves the orchestrator_pod reference to a real pod name.

        Returns:
            str: Actual pod name

        Raises:
            Exception: If pod cannot be resolved
        """
        # If it's already a pod name (doesn't contain '/'), return as-is
        if '/' not in self.orchestrator_pod:
            return self.orchestrator_pod

        # Parse the resource type and name
        resource_type, resource_name = self.orchestrator_pod.split('/', 1)

        # For deployments/statefulsets, get the selector labels and find the pod
        if resource_type in ["deployment", "deploy", "statefulset", "sts"]:
            # Get the label selector from the deployment
            get_selector_cmd = self._kubectl_base_cmd + [
                "get", resource_type, resource_name,
                "-n", self.namespace,
                "-o", "jsonpath={.spec.selector.matchLabels}"
            ]

            try:
                result = subprocess.run(get_selector_cmd, capture_output=True, text=True, check=True)
                # Parse JSON output like {"app":"emulation-manager"}
                import json
                labels = json.loads(result.stdout.strip())

                # Build label selector string: "app=emulation-manager,component=orchestrator"
                label_selector = ",".join([f"{k}={v}" for k, v in labels.items()])

                # Get the first pod matching these labels
                get_pod_cmd = self._kubectl_base_cmd + [
                    "get", "pods",
                    "-n", self.namespace,
                    "-l", label_selector,
                    "-o", "jsonpath={.items[0].metadata.name}"
                ]

                result = subprocess.run(get_pod_cmd, capture_output=True, text=True, check=True)
                pod_name = result.stdout.strip()

                if not pod_name:
                    raise Exception(f"No running pod found for {self.orchestrator_pod}")

                return pod_name

            except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
                raise Exception(f"Failed to resolve pod name for {self.orchestrator_pod}: {e}")

        # For other resource types, just use the name directly
        return resource_name

    def _get_remote_file_size(self, remote_file):
        """
        Get the size of a file on the remote pod.

        Args:
            remote_file: Path to file in the orchestrator pod

        Returns:
            int: File size in bytes

        Raises:
            Exception: If file doesn't exist or size cannot be determined
        """
        size_check_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c", f"stat -c%s {remote_file} 2>/dev/null || echo 0"
        ]

        try:
            result = subprocess.run(size_check_cmd, capture_output=True, text=True, check=True)
            file_size = int(result.stdout.strip())

            if file_size == 0:
                raise FileNotFoundError(f"Remote file not found or is empty: {remote_file}")

            return file_size
        except (subprocess.CalledProcessError, ValueError) as e:
            raise Exception(f"Failed to check file size for {remote_file}: {e}")

    def _split_remote_file(self, remote_file):
        """
        Split a large file on the remote pod into chunks.

        Args:
            remote_file: Path to file in the orchestrator pod

        Returns:
            list[tuple[str, int]]: List of (chunk_path, chunk_size_bytes) tuples

        Raises:
            Exception: If split operation fails
        """
        split_script = f'''
        cd /tmp
        split -b {CHUNK_SIZE_MB}M {remote_file} {remote_file}.part
        echo "SPLIT_FILES:"
        ls -1 {remote_file}.part* | while read f; do
            size=$(stat -c%s "$f")
            echo "$f:$size"
        done
        '''

        split_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c", split_script
        ]

        result = subprocess.run(split_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to split file: {result.stderr}")

        # Parse the split files list
        split_files = []
        for line in result.stdout.split('\n'):
            if line.startswith(f"{remote_file}.part"):
                parts = line.split(':')
                if len(parts) == 2:
                    filename = parts[0]
                    size = int(parts[1])
                    split_files.append((filename, size))

        if not split_files:
            raise Exception("No split files found after splitting operation")

        return split_files

    def _download_file_with_retry(self, remote_path, local_path, max_retries=MAX_DOWNLOAD_RETRIES):
        """
        Download a single file from remote pod with retry logic.

        Args:
            remote_path: Path to file in the orchestrator pod
            local_path: Local path where file should be saved
            max_retries: Maximum number of retry attempts

        Returns:
            bool: True if download successful, False otherwise
        """
        # Resolve deployment/statefulset to actual pod name for kubectl cp
        try:
            pod_name = self._resolve_pod_name()
        except Exception as e:
            print(f"Failed to resolve pod name: {e}")
            return False

        cp_cmd = self._kubectl_base_cmd + [
            "cp",
            "-n", self.namespace,
            f"{pod_name}:{remote_path}",
            local_path
        ]

        for attempt in range(max_retries):
            try:
                subprocess.run(cp_cmd, stderr=subprocess.PIPE, check=True)
                return True  # Success
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode(errors="ignore") if e.stderr else str(e)
                if attempt < max_retries - 1:
                    wait_time = RETRY_BACKOFF_BASE ** attempt
                    print(f"Attempt {attempt+1} failed, retrying in {wait_time}s... ({err})")
                    time.sleep(wait_time)
                else:
                    print(f"Failed to download after {max_retries} attempts: {err}")
                    return False

        return False

    def _reassemble_chunks(self, chunk_paths, output_file):
        """
        Reassemble downloaded chunks into a single file.

        Args:
            chunk_paths: List of paths to chunk files (in order)
            output_file: Path where reassembled file should be saved

        Raises:
            Exception: If reassembly fails
        """
        try:
            with open(output_file, "wb") as f_out:
                for chunk_path in chunk_paths:
                    with open(chunk_path, "rb") as f_in:
                        f_out.write(f_in.read())
                    os.remove(chunk_path)  # Clean up chunk after adding to output
        except Exception as e:
            # Clean up partial output file
            if os.path.exists(output_file):
                os.remove(output_file)
            raise Exception(f"Failed to reassemble chunks: {e}")

    def _cleanup_remote_files(self, file_pattern):
        """
        Remove files matching pattern from remote pod.

        This is a best-effort operation - failures are logged but not raised.

        Args:
            file_pattern: Shell glob pattern for files to remove
        """
        cleanup_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c", f"rm -f {file_pattern}"
        ]
        result = subprocess.run(cleanup_cmd, capture_output=True)
        if result.returncode != 0:
            print(f"Warning: Failed to clean up remote files: {file_pattern}")

    def _download_large_file(self, remote_file, local_file, description="file"):
        """
        Download a file from orchestrator pod with automatic chunking for large files.

        Strategy:
        - Files > LARGE_FILE_THRESHOLD_MB: Split into chunks, download separately, reassemble
        - Files <= LARGE_FILE_THRESHOLD_MB: Direct download

        Args:
            remote_file: Path to file in the orchestrator pod
            local_file: Path where to save the file locally
            description: Description of what's being downloaded (for user messages)

        Returns:
            bool: True if download successful, False otherwise
        """
        print(f"Downloading {description}...")

        # Step 1: Check file size
        try:
            file_size = self._get_remote_file_size(remote_file)
            size_mb = file_size / (1024 * 1024)
            print(f"File size: {size_mb:.1f}MB")
        except Exception as e:
            print(f"Failed to check file size: {e}")
            return False

        # Step 2: Choose download strategy based on file size
        threshold_bytes = LARGE_FILE_THRESHOLD_MB * 1024 * 1024

        if file_size > threshold_bytes:
            # Large file: split, download chunks, reassemble
            if not self._download_large_file_chunked(remote_file, local_file, file_size):
                return False
        else:
            # Small file: direct download
            if not self._download_file_with_retry(remote_file, local_file):
                print(f"Failed to download {description}")
                return False

        # Step 3: Verify the download
        try:
            local_size = os.path.getsize(local_file)
            local_size_mb = local_size / (1024 * 1024)
            print(f"✓ Downloaded {local_size_mb:.1f}MB to {local_file}")

            if local_size != file_size:
                print(f"⚠ Warning: Size mismatch (remote: {file_size}, local: {local_size})")
        except Exception as e:
            print(f"Warning: Could not verify download: {e}")

        return True

    def _download_large_file_chunked(self, remote_file, local_file, file_size):
        """
        Download a large file by splitting into chunks.

        Args:
            remote_file: Path to file in the orchestrator pod
            local_file: Local destination path
            file_size: Size of the file in bytes (for progress tracking)

        Returns:
            bool: True if successful, False otherwise
        """
        print(f"Large file detected (>{LARGE_FILE_THRESHOLD_MB}MB), splitting into {CHUNK_SIZE_MB}MB chunks...")

        # Step 1: Split the file on remote
        try:
            split_files = self._split_remote_file(remote_file)
            print(f"File split into {len(split_files)} chunks")
        except Exception as e:
            print(f"Failed to split file: {e}")
            return False

        # Step 2: Download each chunk
        local_parts = []
        try:
            for i, (remote_part, size) in enumerate(split_files):
                local_part = f"{local_file}.part{chr(97+i)}"  # .partaa, .partab, etc.
                local_parts.append(local_part)

                chunk_size_mb = size / (1024 * 1024)
                print(f"Downloading chunk {i+1}/{len(split_files)} ({chunk_size_mb:.1f}MB)...")

                if not self._download_file_with_retry(remote_part, local_part):
                    print(f"Failed to download chunk {i+1}")
                    # Cleanup partial downloads
                    for cleanup_part in local_parts:
                        if os.path.exists(cleanup_part):
                            os.remove(cleanup_part)
                    return False

        finally:
            # Always clean up remote split files, even if download failed
            self._cleanup_remote_files(f"{remote_file}.part*")

        # Step 3: Reassemble chunks
        try:
            print("Reassembling chunks...")
            self._reassemble_chunks(local_parts, local_file)
        except Exception as e:
            print(f"Failed to reassemble file: {e}")
            return False

        return True

    def download_runner_logs(self, runner_id=None, local_destination="./runner_logs"):
        """
        Download the complete scenario runner output log file.

        This downloads the full /tmp/scenario-runners/{runner_id}/output.log file
        that contains all the output from the batch scenario run.

        Args:
            runner_id: The runner ID (optional, uses saved ID if not provided)
            local_destination: Local directory to save the log file

        Returns:
            bool: True if download successful
        """
        r_id = runner_id or getattr(self, 'runner_id', None)
        if not r_id:
            print("No runner ID provided")
            return False

        remote_file = f"/tmp/scenario-runners/{r_id}/output.log"

        # Create local destination directory
        os.makedirs(local_destination, exist_ok=True)
        local_file = os.path.join(local_destination, f"runner_{r_id}_output.log")

        try:
            return self._download_large_file(
                remote_file=remote_file,
                local_file=local_file,
                description=f"runner logs for {r_id}"
            )
        except Exception as e:
            print(f"Unexpected error during download: {e}")
            if os.path.exists(local_file):
                os.remove(local_file)
            return False

    def stop_scenario_runner(self, runner_id=None):
        """
        Stop a running scenario runner - terminates entire run.

        This will stop the current simulation and prevent further scenarios from running.
        The environment will be cleaned up and ready for a new run.
        """
        r_id = runner_id or getattr(self, 'runner_id', None)
        if not r_id:
            return {"status": "error", "message": "No runner ID provided"}

        # Send SIGTERM to the scenario runner process
        stop_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            RUNNER_DIR=/tmp/scenario-runners/{r_id}

            if [ -f "$RUNNER_DIR/pid" ]; then
                PID=$(cat "$RUNNER_DIR/pid")

                if ps -p $PID > /dev/null 2>&1; then
                    # Send SIGTERM to scenario runner (terminate entire run)
                    kill -TERM $PID

                    # Update status
                    echo '{{"status": "stopping", "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}' > $RUNNER_DIR/status.json

                    echo '{{"status": "stop_signal_sent", "pid": "'$PID'"}}'
                else
                    echo '{{"status": "not_running"}}'
                fi
            else
                echo '{{"status": "not_found"}}'
            fi
            """
        ]

        try:
            result = subprocess.run(stop_cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.strip())
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def skip_scenario_runner(self, runner_id=None):
        """
        Skip the current scenario and continue to next one.

        This will stop the current simulation gracefully but continue with
        the next scenario in the batch.
        """
        r_id = runner_id or getattr(self, 'runner_id', None)
        if not r_id:
            return {"status": "error", "message": "No runner ID provided"}

        # Send SIGUSR1 to the scenario runner process
        skip_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            RUNNER_DIR=/tmp/scenario-runners/{r_id}

            if [ -f "$RUNNER_DIR/pid" ]; then
                PID=$(cat "$RUNNER_DIR/pid")

                if ps -p $PID > /dev/null 2>&1; then
                    # Send SIGUSR1 to scenario runner (skip to next scenario)
                    kill -USR1 $PID

                    echo '{{"status": "skip_signal_sent", "pid": "'$PID'"}}'
                else
                    echo '{{"status": "not_running"}}'
                fi
            else
                echo '{{"status": "not_found"}}'
            fi
            """
        ]

        try:
            result = subprocess.run(skip_cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.strip())
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _get_orchestrator_pod_name(self):
        """Get the actual pod name from deployment"""
        if "deployment/" in self.orchestrator_pod:
            deployment_name = self.orchestrator_pod.split("/")[1]
            cmd = self._kubectl_base_cmd + [
                "get", "pods", "-n", self.namespace,
                "-l", f"app={deployment_name}",
                "-o", "jsonpath={.items[0].metadata.name}"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        else:
            return self.orchestrator_pod


    # The stop is not awaited: the caller polls get_status() for the outcome.
    def stop_simulation(self, simulation_id=None, timeout=30):
        """Stop a running simulation gracefully"""
        sim_id = simulation_id or self.simulation_id

        stop_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c",
            f"""
            SIM_DIR=/tmp/simulations/{sim_id}

            if [ -f "$SIM_DIR/pid" ]; then
                PID=$(cat "$SIM_DIR/pid")

                # First try graceful shutdown with SIGTERM
                if ps -p $PID > /dev/null 2>&1; then
                    # Send SIGTERM to process
                    kill -TERM $PID

                    echo '{{"status": "stopping"}}'
                else
                    echo '{{"status": "not_running"}}'
                fi
            else
                echo '{{"status": "not_found"}}'
            fi
            """
        ]

        result = subprocess.run(stop_cmd, capture_output=True, text=True, check=True)
        print(f"Stop command result: {result.stdout}")

        return {"status": "stopped", "simulation_id": sim_id}

    def download_logs(self, local_destination="./logs", all_logs=False, last_n=None):
        """
        Download logs from the orchestrator container to local machine.

        If all_logs is True, downloads the entire /app/logs directory.
        If last_n is specified, downloads the last N simulation directories.
        Otherwise, downloads the most recent top-level simulation directory.
        """
        print("Preparing to download logs from orchestrator...")

        if all_logs:
            logs_dir = "/app/logs"
            tar_name = "/tmp/logs-all.tar.gz"
            print("Downloading the entire /app/logs directory...")
        else:
            # Determine how many log directories to get
            num_logs = last_n if last_n else 1

            find_sim_logs_script = f'''
            LOGS_DIRS=$(find /app/logs -mindepth 1 -maxdepth 1 -type d | grep -E "[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}" | sort -r | head -{num_logs})
            if [ -z "$LOGS_DIRS" ]; then
                echo "ERROR: No simulation logs found"
                exit 1
            fi
            echo "LOGS_DIRS:$LOGS_DIRS"
            '''

            find_cmd = self._kubectl_base_cmd + [
                "exec", "-n", self.namespace,
                self.orchestrator_pod, "--",
                "sh", "-c", find_sim_logs_script
            ]

            result = subprocess.run(find_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print("Could not find simulation logs")
                return False

            logs_dirs = []
            found_marker = False
            for line in result.stdout.split('\n'):
                if line.startswith("LOGS_DIRS:"):
                    # First directory is on the same line as the marker
                    first_dir = line.split(":", 1)[1].strip()
                    if first_dir:
                        logs_dirs.append(first_dir)
                    found_marker = True
                elif found_marker and line.strip() and line.startswith("/app/logs"):
                    # Subsequent directories on following lines
                    logs_dirs.append(line.strip())

            if not logs_dirs:
                print("Could not determine logs directory")
                return False

            if len(logs_dirs) == 1:
                print(f"Found logs directory: {logs_dirs[0]}")
            else:
                print(f"Found {len(logs_dirs)} log directories:")
                for d in logs_dirs:
                    print(f"  - {d}")

            tar_name = "/tmp/logs-latest.tar.gz"

        if all_logs:
            # For all_logs, archive the entire logs directory
            tar_script = f'''
            TAR_FILE="{tar_name}"
            # Create archive while being tolerant to files changing during read
            # --warning=no-file-changed avoids non-zero exit when files are updated during archiving
            # --ignore-failed-read skips files that disappear mid-archive
            tar --warning=no-file-changed --ignore-failed-read -czf "$TAR_FILE" -C "/" "{logs_dir.lstrip('/')}"
            if [ -f "$TAR_FILE" ]; then
                SIZE=$(ls -lh "$TAR_FILE" | awk '{{print $5}}')
                echo "Created archive: $TAR_FILE ($SIZE)"
            else
                echo "ERROR: Failed to create archive"
                exit 1
            fi
            '''
        else:
            # For specific directories, create tar command with all found directories
            dirs_for_tar = ' '.join([f'"{d.lstrip("/")}"' for d in logs_dirs])
            tar_script = f'''
            TAR_FILE="{tar_name}"
            # Create archive while being tolerant to files changing during read
            # --warning=no-file-changed avoids non-zero exit when files are updated during archiving
            # --ignore-failed-read skips files that disappear mid-archive
            tar --warning=no-file-changed --ignore-failed-read -czf "$TAR_FILE" -C "/" {dirs_for_tar}
            if [ -f "$TAR_FILE" ]; then
                SIZE=$(ls -lh "$TAR_FILE" | awk '{{print $5}}')
                echo "Created archive: $TAR_FILE ($SIZE)"
            else
                echo "ERROR: Failed to create archive"
                exit 1
            fi
            '''

        tar_cmd = self._kubectl_base_cmd + [
            "exec", "-n", self.namespace,
            self.orchestrator_pod, "--",
            "sh", "-c", tar_script
        ]

        print("Creating archive of logs...")
        result = subprocess.run(tar_cmd, capture_output=True, text=True)
        print(result.stdout)

        if result.returncode != 0:
            print("Failed to create logs archive")
            return False

        local_tar = tar_name.replace("/tmp/", "/tmp/local-")

        # Use the unified download utility function
        success = self._download_large_file(
            remote_file=tar_name,
            local_file=local_tar,
            description="logs archive"
        )

        if not success:
            return False

        os.makedirs(local_destination, exist_ok=True)

        extract_cmd = ["tar", "-xzf", local_tar, "-C", local_destination]
        result = subprocess.run(extract_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            print(f"✓ Logs extracted to {local_destination}")
            os.remove(local_tar)
            cleanup_cmd = self._kubectl_base_cmd + [
                "exec", "-n", self.namespace,
                self.orchestrator_pod, "--",
                "rm", "-f", tar_name
            ]
            subprocess.run(cleanup_cmd, capture_output=True)
            return True
        else:
            print(f"Failed to extract archive: {result.stderr}")
            return False

    def deploy_manager(self, image_prefix="", wait_ready=True):
        """
        Deploy the simulation manager/orchestrator to the cluster if not already present.

        Args:
            image_prefix: Image prefix/registry (e.g., "myregistry.io/project/")
            wait_ready: Wait for the deployment to be ready

        Returns:
            bool: True if deployment successful
        """
        manager_image = f"{image_prefix}emulator-manager"

        # # Check if deployment already exists
        # check_cmd = self._kubectl_base_cmd + [
        #     "get", "deployment", deployment_name,
        #     "-n", self.namespace,
        #     "--ignore-not-found=true"
        # ]
        #
        # result = subprocess.run(check_cmd, capture_output=True, text=True)
        # if deployment_name in result.stdout:
        #     print(f"✓ Manager deployment already exists in namespace {self.namespace}")
        #     return True

        print(f"Deploying simulation manager to namespace {self.namespace}...")

        # # Create namespace if it doesn't exist
        # ns_cmd = self._kubectl_base_cmd + [
        #     "create", "namespace", self.namespace,
        #     "--dry-run=client", "-o", "yaml"
        # ]
        # subprocess.run(ns_cmd + ["| kubectl apply -f -"], shell=True)

        # Check if pre-created manifests exist
        manifest_dir = "./containers/emulator-manager"
        deployment_file = os.path.join(manifest_dir, "deployment.yaml")

        if os.path.exists(deployment_file):
            # Use pre-created manifests
            print("Using pre-created manifests from containers/emulator-manager/...")

            # Apply all manifests
            manifest_files = [
                "role.yaml",
                "rolebinding.yaml",
                "serviceaccount.yaml",
                "pvc.yaml",
                "deployment.yaml"
            ]

            for manifest in manifest_files:
                manifest_path = os.path.join(manifest_dir, manifest)
                if os.path.exists(manifest_path):
                    if manifest == "deployment.yaml" and image_prefix:
                        with open(manifest_path, encoding="utf-8") as source:
                            deployment = yaml.safe_load(source)
                        pod_spec = deployment["spec"]["template"]["spec"]
                        for container in pod_spec["containers"]:
                            if container["name"] == "manager":
                                original_image = container["image"]
                                container["image"] = manager_image
                                break
                        else:
                            raise CoinjoinEmulatorError(
                                f"Container 'manager' not found in {manifest_path}"
                            )
                        # Init containers that run the manager image follow the override.
                        for container in pod_spec.get("initContainers", []):
                            if container.get("image") == original_image:
                                container["image"] = manager_image
                        subprocess.run(
                            self._kubectl_base_cmd
                            + ["apply", "-f", "-", "-n", self.namespace],
                            input=yaml.safe_dump(deployment),
                            text=True,
                            check=True,
                        )
                        continue

                    apply_cmd = self._kubectl_base_cmd + [
                        "apply", "-f", manifest_path,
                        "-n", self.namespace
                    ]
                    subprocess.run(apply_cmd, check=True)
                else:
                    print(f"Warning: {manifest} not found in {manifest_dir}")

        if wait_ready:
            # rollout status only understands controllers; a bare pod reference
            # has to be waited for with `kubectl wait`.
            if self.orchestrator_pod.startswith("deployment/"):
                ready_cmd = ["rollout", "status", self.orchestrator_pod, "--timeout=300s"]
            else:
                ready_cmd = [
                    "wait", "--for=condition=Ready", f"pod/{self.orchestrator_pod}", "--timeout=300s"
                ]
            result = subprocess.run(
                self._kubectl_base_cmd + ready_cmd + ["-n", self.namespace],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                print(f"Orchestrator did not become ready: {result.stderr.strip()}")
                return False

        return True
