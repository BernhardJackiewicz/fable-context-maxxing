"""Acceptance tests for contract lab-D1: scenarios and seeded properties."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
RP = str(BIN / "red_proof.py")
sys.path.insert(0, str(BIN))

import red_proof  # noqa: E402


def make_env_repo(base, config, home):
    repo = base / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo, check=True)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config)
    env["HOME"] = str(home)
    return repo, env


def rp_run(env, repo, *args, stdin_data=None):
    return subprocess.run([sys.executable, RP, *args],
                          input=stdin_data, capture_output=True, text=True,
                          env=env, cwd=str(repo))


def test_feature_files_are_nonproduction(tmp_path):
    # hook allows editing a .feature file with no active cycle, while a
    # production .py file in the same repo is still denied
    home_base = Path(tempfile.mkdtemp(prefix=".lab-d1-", dir=Path.home()))
    try:
        config = tmp_path / "config"
        home = tmp_path / "home"
        home.mkdir()
        repo, env = make_env_repo(home_base, config, home)
        feature = json.dumps(
            {"tool_input": {"file_path": str(repo / "login.feature")}})
        denied_py = json.dumps(
            {"tool_input": {"file_path": str(repo / "prod.py")}})
        r = rp_run(env, repo, "hook", "edit", stdin_data=feature)
        assert r.returncode == 0 and r.stdout.strip() == ""
        r = rp_run(env, repo, "hook", "edit", stdin_data=denied_py)
        assert '"deny"' in r.stdout
    finally:
        shutil.rmtree(home_base, ignore_errors=True)


def test_is_nonprod_and_fingerprints_for_feature(tmp_path):
    assert red_proof.is_nonprod("login.feature")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "prod.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"], cwd=repo, check=True)
    fp = red_proof.fingerprint(str(repo))
    pf = red_proof.production_fingerprint(str(repo))
    (repo / "login.feature").write_text("Feature: login\n")
    assert red_proof.fingerprint(str(repo)) != fp
    assert red_proof.production_fingerprint(str(repo)) == pf


@pytest.fixture
def cycle(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo, env = make_env_repo(tmp_path, config, home)
    (repo / "contract.md").write_text("# c\n")

    def rp(*args):
        return rp_run(env, repo, *args)

    def state():
        files = list((config / "red-proof" / "state").glob("*.json"))
        return json.loads(files[0].read_text())

    return rp, state, repo


def test_scenario_red_type(cycle):
    rp, state, repo = cycle
    assert rp("contract", "--file", "contract.md").returncode == 0
    r = rp("red", "--test", "features/login.feature", "--type", "scenario",
           "--expected", "missing step definition", "--",
           sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0, r.stdout + r.stderr
    assert state()["red_proofs"][0]["red_type"] == "scenario"
    r = rp("red", "--test", "t", "--type", "wild", "--expected", "x", "--",
           sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode != 0


def frozen_cycle(rp, repo, require):
    assert rp("contract", "--file", "contract.md",
              "--require", require).returncode == 0
    r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
           "--", sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0
    (repo / "test_acc.py").write_text("def test_acc():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    assert rp("freeze").returncode == 0


def test_property_check_demands_a_seed(cycle):
    rp, state, repo = cycle
    frozen_cycle(rp, repo, "property")
    assert "property" in state()["required_evidence"]
    r = rp("check", "property", "--", sys.executable, "-c", "pass")
    assert r.returncode != 0
    assert "seed" in (r.stdout + r.stderr).lower()
    assert "property" not in state()["evidence"]


def test_property_check_records_the_seed(cycle):
    rp, state, repo = cycle
    frozen_cycle(rp, repo, "property")
    r = rp("check", "property", "--",
           "env", "HYPOTHESIS_SEED=1234", sys.executable, "-c", "pass")
    assert r.returncode == 0, r.stdout + r.stderr
    ev = state()["evidence"]["property"]
    assert ev["metrics"]["hypothesis_seed"] == 1234.0
    r = rp("contract", "--file", "contract.md", "--require", "property")
    assert r.returncode == 0
    frozen_cycle(rp, repo, "property")
    r = rp("check", "property", "--",
           sys.executable, "-c", "print('--hypothesis-seed=77 ok')")
    assert r.returncode == 0, r.stdout + r.stderr
    assert state()["evidence"]["property"]["metrics"]["hypothesis_seed"] == 77.0
