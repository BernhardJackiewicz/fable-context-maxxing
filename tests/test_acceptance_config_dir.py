"""Acceptance tests for contract lab-A1: base-directory resolution.

Self-contained on purpose (no conftest dependency): these tests are the
frozen acceptance surface for the CLAUDE_CONFIG_DIR change.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

RP = str(Path(__file__).resolve().parents[1] / "bin" / "red_proof.py")


def run_rp(args, env_overrides, cwd, stdin_data=None):
    env = os.environ.copy()
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, RP, *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def make_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )
    return path


@pytest.fixture
def home_repo():
    # Deny-path tests need a repo outside the allowlisted prefixes
    # (/tmp, /private/tmp, /var/folders, <home>/.claude), so it must
    # live under the real home directory, not under pytest's tmp_path.
    base = Path(tempfile.mkdtemp(prefix=".lab-a1-accept-", dir=Path.home()))
    try:
        yield make_repo(base / "repo")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_exempt_writes_under_config_dir(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path / "repo")

    r = run_rp(
        ["exempt", "--reason", "acceptance test"],
        {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home)},
        cwd=repo,
    )

    assert r.returncode == 0, r.stderr
    assert (config / "red-proof" / "exemptions.log").exists()
    state_dir = config / "red-proof" / "state"
    assert state_dir.is_dir() and list(state_dir.glob("*.json"))
    assert not (home / ".claude").exists()


def test_hook_edit_uses_config_dir_state(tmp_path, home_repo):
    config = tmp_path / "config"
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    payload = json.dumps(
        {"tool_input": {"file_path": str(home_repo / "prod.py")}}
    )

    env_a = {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home_a)}
    denied = run_rp(["hook", "edit"], env_a, cwd=home_repo, stdin_data=payload)
    assert denied.returncode == 0, denied.stderr
    assert '"deny"' in denied.stdout

    r = run_rp(["exempt", "--reason", "acceptance test"], env_a, cwd=home_repo)
    assert r.returncode == 0, r.stderr

    # Same config dir but a DIFFERENT home: the hook must honor the
    # exemption, proving state resolution goes through CLAUDE_CONFIG_DIR
    # and not through the home directory.
    env_b = {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home_b)}
    allowed = run_rp(["hook", "edit"], env_b, cwd=home_repo, stdin_data=payload)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == ""


def test_default_home_claude_without_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path / "repo")

    r = run_rp(["exempt", "--reason", "acceptance test"], {"HOME": str(home)}, cwd=repo)

    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "red-proof" / "exemptions.log").exists()


def test_hook_error_log_under_config_dir(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path / "repo")

    r = run_rp(
        ["hook", "edit"],
        {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home)},
        cwd=repo,
        stdin_data="this is not json",
    )

    # fail-open: exit 0, error logged under the resolved base directory
    assert r.returncode == 0
    assert (config / "red-proof" / "error.log").exists()
    assert not (home / ".claude").exists()
