"""Run the coordinator entrypoint with a local executable and no real sleeps."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "containers/wasabi-coordinator/2.6.0/run.sh"
CONFIG = {"Network": "RegTest", "MiningFeeRate": 5}


@pytest.fixture
def entrypoint(tmp_path):
    # Relocate only the container filesystem; execute the actual shell logic.
    script = tmp_path / "run.sh"
    script.write_text(ENTRYPOINT.read_text().replace("/home/wasabi", str(tmp_path)), encoding="utf-8")
    binary = tmp_path / "WalletWasabi.Coordinator"
    binary.write_text(
        '#!/bin/sh\nset -eu\ncp .walletwasabi/coordinator/Config.json started.json\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    (tmp_path / "Config.json").write_text('{"Network": "RegTest"}', encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return script, {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_the_coordinator_waits_for_confirmation_even_when_the_config_file_exists(entrypoint, tmp_path):
    script, env = entrypoint
    (tmp_path / "polls").write_text("0", encoding="utf-8")
    sleep = tmp_path / "bin/sleep"
    sleep.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "test ! -e started.json\n"
        "count=$(cat polls)\n"
        "count=$((count + 1))\n"
        "echo $count > polls\n"
        "case $count in\n"
        "  20) printf '{\"MiningFeeRate\":' > coordinator-config.json ;;\n"
        f"  21) printf '%s' '{json.dumps(CONFIG)}' > coordinator-config.json ;;\n"
        "  22) touch coordinator-config.ready ;;\n"
        "esac\n"
        "test $count -le 22\n",
        encoding="utf-8",
    )
    sleep.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=3
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "started.json").read_text()) == CONFIG
    assert (tmp_path / "polls").read_text().strip() == "22"
    assert not (tmp_path / "coordinator-config.ready").exists()


def test_a_config_uploaded_before_the_entrypoint_starts_is_preserved(entrypoint, tmp_path):
    script, env = entrypoint
    (tmp_path / "coordinator-config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    (tmp_path / "coordinator-config.ready").touch()

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=3
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "started.json").read_text()) == CONFIG


def test_a_missing_confirmation_times_out_without_starting_the_coordinator(entrypoint, tmp_path):
    script, env = entrypoint
    (tmp_path / "coordinator-config.json").write_text(json.dumps(CONFIG), encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        env={**env, "WASABI_COORDINATOR_CONFIG_TIMEOUT": "0"},
        capture_output=True, text=True, timeout=3,
    )

    assert result.returncode == 1
    assert "Timed out waiting for coordinator configuration upload" in result.stderr
    assert not (tmp_path / "started.json").exists()
