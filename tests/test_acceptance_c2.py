"""Acceptance tests for contract lab-C2: the worktree snapshot guard."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RP = str(Path(__file__).resolve().parents[1] / "bin" / "red_proof.py")


@pytest.fixture
def cycle(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo, check=True)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config)
    env["HOME"] = str(home)
    (repo / "contract.md").write_text("# c\n")

    def rp(*args):
        return subprocess.run([sys.executable, RP, *args],
                              capture_output=True, text=True,
                              env=env, cwd=str(repo))

    def state():
        files = list((config / "red-proof" / "state").glob("*.json"))
        assert len(files) == 1
        return json.loads(files[0].read_text())

    def start(max_attempts=None):
        args = ["contract", "--file", "contract.md"]
        if max_attempts is not None:
            args += ["--max-attempts", str(max_attempts)]
        assert rp(*args).returncode == 0
        r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
               "--", sys.executable, "-c", "raise SystemExit(1)")
        assert r.returncode == 0, r.stdout + r.stderr
        (repo / "test_acc.py").write_text("def test_acc():\n    assert True\n")
        subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
        assert rp("freeze").returncode == 0

    return rp, state, repo, start


FAIL_WITH_MARKER = (
    "import pathlib, sys;"
    "pathlib.Path('ran.marker').touch();"
    "sys.exit(1)"
)


def test_failed_check_records_attempt(cycle):
    rp, state, repo, start = cycle
    start()
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER)
    assert r.returncode != 0
    entry = state()["gate_attempts"]["targeted"]
    assert entry["count"] == 1
    assert entry["fail_fingerprint"]


def test_unchanged_worktree_skips_execution(cycle):
    rp, state, repo, start = cycle
    start()
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    (repo / "ran.marker").unlink()
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER)
    assert r.returncode != 0
    assert not (repo / "ran.marker").exists(), "command must not run again"
    out = r.stdout + r.stderr
    assert "unchanged" in out
    assert "2" in out
    assert state()["gate_attempts"]["targeted"]["count"] == 2


def test_any_change_rearms_the_check(cycle):
    rp, state, repo, start = cycle
    start()
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    (repo / "ran.marker").unlink()
    (repo / "prod.py").write_text("x = 1\n")
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER)
    assert r.returncode != 0
    assert (repo / "ran.marker").exists(), "command must run after a change"


def test_green_check_clears_attempts(cycle):
    rp, state, repo, start = cycle
    start()
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    (repo / "ran.marker").unlink()
    (repo / "prod.py").write_text("x = 1\n")
    assert rp("check", "targeted", "--",
              sys.executable, "-c", "pass").returncode == 0
    assert "targeted" not in state().get("gate_attempts", {})


def test_max_attempts_escalates_to_replanning(cycle):
    rp, state, repo, start = cycle
    start(max_attempts=2)
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    (repo / "ran.marker").unlink()
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER)
    assert r.returncode != 0
    assert "re-plan" in (r.stdout + r.stderr)


def test_counters_are_per_check_and_contract_resets(cycle):
    rp, state, repo, start = cycle
    start()
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    assert rp("check", "static", "--",
              sys.executable, "-c", "raise SystemExit(1)").returncode != 0
    attempts = state()["gate_attempts"]
    assert attempts["targeted"]["count"] == 1
    assert attempts["static"]["count"] == 1
    assert rp("contract", "--file", "contract.md").returncode == 0
    assert state().get("gate_attempts", {}) == {}
