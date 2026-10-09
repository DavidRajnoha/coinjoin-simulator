import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from manager import remote_cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_saved_run_wins_over_the_older_cli_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".runner-ns").write_text("old-runner\n")
    remote_cli._save_run("ns", "single", "sim-1")  # pylint: disable=protected-access

    assert remote_cli._load_run("ns") == ("single", "sim-1")  # pylint: disable=protected-access


def test_runs_started_by_the_older_clis_are_found(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".simulation-a").write_text("sim-7\n")
    (tmp_path / ".runner-b").write_text("runner-3\n")

    assert remote_cli._load_run("a") == ("single", "sim-7")  # pylint: disable=protected-access
    assert remote_cli._load_run("b") == ("batch", "runner-3")  # pylint: disable=protected-access
    assert remote_cli._load_run("c") == (None, None)  # pylint: disable=protected-access


def test_status_routes_a_batch_to_the_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    remote_cli._save_run("ns", "batch", "runner-9")  # pylint: disable=protected-access
    args = remote_cli.build_parser().parse_args(["--namespace", "ns", "status"])

    with patch.object(remote_cli.manager_remote_batch, "runner_status", return_value=True) as status:
        assert args.func(args)

    assert status.call_args.args[0].runner_id == "runner-9"


def test_run_requires_exactly_one_scenario_source() -> None:
    parser = remote_cli.build_parser()
    neither = parser.parse_args(["run"])
    both = parser.parse_args(["run", "--scenario", "a.json", "--scenario-dir", "dir"])

    assert not neither.func(neither)
    assert not both.func(both)


def test_runs_as_a_script_from_the_repository_root() -> None:
    result = subprocess.run(
        [sys.executable, "manager/remote_cli.py", "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "download-logs" in result.stdout
