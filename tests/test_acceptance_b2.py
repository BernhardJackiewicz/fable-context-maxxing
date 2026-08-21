"""Acceptance tests for contract lab-B2: output capture, metrics, --min."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RP = str(Path(__file__).resolve().parents[1] / "bin" / "red_proof.py")


@pytest.fixture
def cycle(tmp_path):
    """A repo driven to TESTS_FROZEN with an isolated config dir."""
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

    def rp(*args):
        return subprocess.run(
            [sys.executable, RP, *args],
            capture_output=True, text=True, env=env, cwd=str(repo),
        )

    r = rp("contract", "--file", "contract.md")
    assert r.returncode == 0, r.stdout + r.stderr
    r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
           "--", sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0, r.stdout + r.stderr
    (repo / "test_acc.py").write_text("def test_a():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    r = rp("freeze")
    assert r.returncode == 0, r.stdout + r.stderr

    def state():
        files = list((config / "red-proof" / "state").glob("*.json"))
        assert len(files) == 1
        return json.loads(files[0].read_text())

    return rp, state


PRINT_TOTAL_91 = (
    "import sys; print('py    120  10  91%'); print('TOTAL  120  10  91%')"
)
PRINT_TOTAL_84 = "print('TOTAL  120  18  84%')"


def test_check_records_output_tail_and_passes_through(cycle):
    rp, state = cycle
    r = rp("check", "targeted", "--",
           sys.executable, "-c", "print('hello tail marker')")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hello tail marker" in r.stdout
    ev = state()["evidence"]["targeted"]
    assert "hello tail marker" in ev["output_tail"]


def test_coverage_extractor_and_min_pass(cycle):
    rp, state = cycle
    r = rp("check", "coverage", "--min", "85", "--",
           sys.executable, "-c", PRINT_TOTAL_91)
    assert r.returncode == 0, r.stdout + r.stderr
    ev = state()["evidence"]["coverage"]
    assert ev["metrics"] == {"coverage_percent": 91.0}
    assert ev["min"] == 85.0


def test_coverage_below_min_fails_no_evidence(cycle):
    rp, state = cycle
    r = rp("check", "coverage", "--min", "85", "--",
           sys.executable, "-c", PRINT_TOTAL_84)
    assert r.returncode != 0
    assert "84" in (r.stdout + r.stderr)
    assert "coverage" not in state()["evidence"]


def test_min_without_extractable_metric_fails(cycle):
    rp, state = cycle
    r = rp("check", "coverage", "--min", "85", "--",
           sys.executable, "-c", "print('no percentages here')")
    assert r.returncode != 0
    assert "coverage" not in state()["evidence"]


def test_min_on_check_without_extractor_fails(cycle):
    rp, state = cycle
    r = rp("check", "static", "--min", "5", "--",
           sys.executable, "-c", "pass")
    assert r.returncode != 0
    assert "static" not in state()["evidence"]


def test_failed_command_records_no_evidence(cycle):
    rp, state = cycle
    r = rp("check", "targeted", "--",
           sys.executable, "-c", "raise SystemExit(2)")
    assert r.returncode != 0
    assert "targeted" not in state()["evidence"]


def test_require_coverage_accepted(cycle):
    rp, state = cycle
    r = rp("contract", "--file", "contract.md", "--require", "coverage")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "coverage" in state()["required_evidence"]
