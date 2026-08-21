"""Acceptance tests for contract lab-C3: the mutation gate."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
RP = str(BIN / "red_proof.py")
sys.path.insert(0, str(BIN))

import red_proof  # noqa: E402


def test_registry_entry_has_extractor():
    spec = red_proof.CHECKS["mutation"]
    assert spec["staleness"] == "production"
    assert callable(spec["extract"])


def test_extract_mutation_formats():
    assert red_proof.extract_mutation("Killed 12 out of 15") == {
        "mutation_score": 80.0}
    assert red_proof.extract_mutation(
        "junk\n12/15  KILLED\nmore") == {"mutation_score": 80.0}
    assert red_proof.extract_mutation("Killed 15 out of 15") == {
        "mutation_score": 100.0}
    assert red_proof.extract_mutation("no mutation talk here") is None
    assert red_proof.extract_mutation("") is None


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
              "--require", "mutation").returncode == 0
    r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
           "--", sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0
    (repo / "test_acc.py").write_text("def test_acc():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    assert rp("freeze").returncode == 0
    return rp, state, repo


def test_mutation_min_pass_and_fail(cycle):
    rp, state, repo = cycle
    r = rp("check", "mutation", "--min", "80", "--",
           sys.executable, "-c", "print('Killed 13 out of 15')")
    assert r.returncode == 0, r.stdout + r.stderr
    ev = state()["evidence"]["mutation"]
    assert abs(ev["metrics"]["mutation_score"] - 86.6667) < 0.001
    r = rp("check", "mutation", "--min", "90", "--",
           sys.executable, "-c", "print('Killed 12 out of 15')")
    assert r.returncode != 0


def test_mutmut_artifacts_do_not_touch_fingerprints(cycle):
    rp, state, repo = cycle
    fp = red_proof.fingerprint(str(repo))
    pf = red_proof.production_fingerprint(str(repo))
    (repo / "mutants").mkdir()
    (repo / "mutants" / "m1.py").write_text("x = 1\n")
    (repo / ".mutmut-cache").write_text("cache\n")
    assert red_proof.fingerprint(str(repo)) == fp
    assert red_proof.production_fingerprint(str(repo)) == pf
