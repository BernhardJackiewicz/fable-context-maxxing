"""Acceptance tests for contract lab-B1: required_evidence + CHECKS registry.

Self-contained frozen surface: drives the CLI as a subprocess with an
isolated CLAUDE_CONFIG_DIR and reads the state file directly.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RP = str(Path(__file__).resolve().parents[1] / "bin" / "red_proof.py")


@pytest.fixture
def env_repo(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config)
    env["HOME"] = str(home)
    (repo / "contract.md").write_text("# c\n")
    return env, repo, config


def rp(env, repo, *args, stdin_data=None):
    return subprocess.run(
        [sys.executable, RP, *args],
        input=stdin_data, capture_output=True, text=True,
        env=env, cwd=str(repo),
    )


def read_state(config):
    files = list((config / "red-proof" / "state").glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def start_frozen_cycle(env, repo, require=None):
    """contract -> red -> freeze, returns after phase TESTS_FROZEN."""
    args = ["contract", "--file", "contract.md"]
    if require:
        args += ["--require", require]
    r = rp(env, repo, *args)
    assert r.returncode == 0, r.stdout + r.stderr
    r = rp(env, repo, "red", "--test", "t", "--type", "behavior",
           "--expected", "fails", "--",
           sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0, r.stdout + r.stderr
    (repo / "test_acc.py").write_text("def test_a():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    r = rp(env, repo, "freeze")
    assert r.returncode == 0, r.stdout + r.stderr


def green_checks(env, repo, names=("targeted", "full-suite")):
    for name in names:
        r = rp(env, repo, "check", name, "--",
               sys.executable, "-c", "pass")
        assert r.returncode == 0, name + ": " + r.stdout + r.stderr
    r = rp(env, repo, "attest", "--diff-reviewed", "--contract-ok")
    assert r.returncode == 0, r.stdout + r.stderr


def test_require_writes_state(env_repo):
    env, repo, config = env_repo
    r = rp(env, repo, "contract", "--file", "contract.md",
           "--require", "static")
    assert r.returncode == 0, r.stdout + r.stderr
    state = read_state(config)
    assert state["required_evidence"] == [
        "targeted", "full_suite", "attest", "static"]


def test_require_unknown_rejected(env_repo):
    env, repo, config = env_repo
    r = rp(env, repo, "contract", "--file", "contract.md",
           "--require", "nosuch")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "nosuch" in out
    assert "static" in out  # valid names are listed


def test_check_unknown_name_rejected(env_repo):
    env, repo, config = env_repo
    start_frozen_cycle(env, repo)
    r = rp(env, repo, "check", "bogus", "--", sys.executable, "-c", "pass")
    assert r.returncode != 0
    assert "bogus" in (r.stdout + r.stderr)


def test_commit_gate_requires_declared_key(env_repo):
    env, repo, config = env_repo
    start_frozen_cycle(env, repo, require="static")
    green_checks(env, repo)
    r = rp(env, repo, "commit-gate")
    assert r.returncode != 0
    assert "static" in (r.stdout + r.stderr)
    r = rp(env, repo, "check", "static", "--", sys.executable, "-c", "pass")
    assert r.returncode == 0, r.stdout + r.stderr
    r = rp(env, repo, "commit-gate")
    assert r.returncode == 0, r.stdout + r.stderr


def test_default_cycle_unchanged(env_repo):
    env, repo, config = env_repo
    start_frozen_cycle(env, repo)
    green_checks(env, repo)
    r = rp(env, repo, "commit-gate")
    assert r.returncode == 0, r.stdout + r.stderr


def test_old_state_without_field_falls_back(env_repo):
    env, repo, config = env_repo
    start_frozen_cycle(env, repo)
    green_checks(env, repo)
    state_file = next((config / "red-proof" / "state").glob("*.json"))
    state = json.loads(state_file.read_text())
    state.pop("required_evidence", None)
    state_file.write_text(json.dumps(state))
    r = rp(env, repo, "commit-gate")
    assert r.returncode == 0, r.stdout + r.stderr


def test_status_shows_required(env_repo):
    env, repo, config = env_repo
    r = rp(env, repo, "contract", "--file", "contract.md",
           "--require", "static")
    assert r.returncode == 0
    r = rp(env, repo, "status")
    assert r.returncode == 0
    assert "required_evidence" in r.stdout
    assert "static" in r.stdout
