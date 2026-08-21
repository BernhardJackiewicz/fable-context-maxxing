"""Acceptance tests for contract lab-F2: property --min and guard command."""

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
        return json.loads(files[0].read_text())

    assert rp("contract", "--file", "contract.md",
              "--require", "property").returncode == 0
    r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
           "--", sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0
    (repo / "test_acc.py").write_text("def test_acc():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    assert rp("freeze").returncode == 0
    return rp, state, repo


def test_property_rejects_min(cycle):
    rp, state, repo = cycle
    r = rp("check", "property", "--min", "5", "--",
           "env", "HYPOTHESIS_SEED=1234", sys.executable, "-c", "pass")
    assert r.returncode != 0
    assert "property" not in state()["evidence"]
    assert "property" not in state().get("gate_attempts", {})


FAIL_WITH_MARKER = (
    "import pathlib, sys;"
    "pathlib.Path('ran.marker').touch();"
    "sys.exit(1)"
)

FAIL_WITH_MARKER2 = (
    "import pathlib, sys;"
    "pathlib.Path('ran2.marker').touch();"
    "sys.exit(1)"
)


def test_guard_blocks_same_command_but_allows_a_corrected_one(cycle):
    rp, state, repo = cycle
    assert rp("check", "targeted", "--",
              sys.executable, "-c", FAIL_WITH_MARKER).returncode != 0
    (repo / "ran.marker").unlink()
    # same command, unchanged tree: blocked, counted
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER)
    assert r.returncode != 0
    assert not (repo / "ran.marker").exists()
    assert "unchanged" in (r.stdout + r.stderr)
    # different command, unchanged tree: executes
    r = rp("check", "targeted", "--", sys.executable, "-c", FAIL_WITH_MARKER2)
    assert (repo / "ran2.marker").exists(), "corrected command must run"
