"""Acceptance tests for contract lab-C1: production fingerprint + staleness."""

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


def test_is_nonprod_matches_hook_classification():
    assert red_proof.is_nonprod("tests/foo.py")
    assert red_proof.is_nonprod("docs/gen.py")
    assert red_proof.is_nonprod("examples/demo.py")
    assert red_proof.is_nonprod("test_x.py")
    assert red_proof.is_nonprod("pkg/test_y.py")
    assert red_proof.is_nonprod("README.md")
    assert not red_proof.is_nonprod("app.py")
    assert not red_proof.is_nonprod("bench/bench.py")


@pytest.fixture
def cycle(tmp_path):
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "prod.py").write_text("x = 1\n")
    (repo / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / "notes.md").write_text("notes\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"], cwd=repo, check=True)
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

    return rp, state, repo


def test_production_fingerprint_ignores_nonprod_changes(cycle):
    rp, state, repo = cycle
    fp1 = red_proof.fingerprint(str(repo))
    pf1 = red_proof.production_fingerprint(str(repo))
    (repo / "test_a.py").write_text("def test_a():\n    assert 1\n")
    (repo / "notes.md").write_text("more notes\n")
    assert red_proof.fingerprint(str(repo)) != fp1
    assert red_proof.production_fingerprint(str(repo)) == pf1
    (repo / "prod.py").write_text("x = 2\n")
    assert red_proof.production_fingerprint(str(repo)) != pf1


def drive_to_frozen(rp, repo, require=None):
    args = ["contract", "--file", "contract.md"]
    if require:
        args += ["--require", require]
    assert rp(*args).returncode == 0
    r = rp("red", "--test", "t", "--type", "behavior", "--expected", "f",
           "--", sys.executable, "-c", "raise SystemExit(1)")
    assert r.returncode == 0, r.stdout + r.stderr
    (repo / "test_acc.py").write_text("def test_acc():\n    assert True\n")
    subprocess.run(["git", "add", "test_acc.py"], cwd=repo, check=True)
    assert rp("freeze").returncode == 0


def green(rp, names):
    for name in names:
        r = rp("check", name, "--", sys.executable, "-c", "pass")
        assert r.returncode == 0, name + ": " + r.stdout + r.stderr
    r = rp("attest", "--diff-reviewed", "--contract-ok")
    assert r.returncode == 0


def test_checks_store_both_fingerprints(cycle):
    rp, state, repo = cycle
    drive_to_frozen(rp, repo)
    r = rp("check", "targeted", "--", sys.executable, "-c", "pass")
    assert r.returncode == 0
    ev = state()["evidence"]["targeted"]
    assert ev["fingerprint"]
    assert ev["production_fingerprint"]


def test_production_staleness_end_to_end(cycle):
    rp, state, repo = cycle
    drive_to_frozen(rp, repo, require="mutation")
    green(rp, ["targeted", "full-suite", "mutation"])
    assert rp("commit-gate").returncode == 0
    # test-only change: strict keys go stale, mutation survives
    (repo / "test_extra.py").write_text("def test_e():\n    assert True\n")
    r = rp("commit-gate")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "targeted" in out
    assert "mutation" not in out
    green(rp, ["targeted", "full-suite"])
    assert rp("commit-gate").returncode == 0
    # production change: mutation goes stale too
    (repo / "prod.py").write_text("x = 3\n")
    r = rp("commit-gate")
    assert r.returncode != 0
    assert "mutation" in (r.stdout + r.stderr)


def test_old_evidence_without_production_fp_is_strict(cycle):
    rp, state, repo = cycle
    drive_to_frozen(rp, repo, require="mutation")
    green(rp, ["targeted", "full-suite", "mutation"])
    config_state = next(
        (Path(str(repo)).parents[0] / "config" / "red-proof" / "state")
        .glob("*.json"))
    doc = json.loads(config_state.read_text())
    del doc["evidence"]["mutation"]["production_fingerprint"]
    config_state.write_text(json.dumps(doc))
    (repo / "test_extra.py").write_text("def test_e():\n    assert True\n")
    r = rp("commit-gate")
    assert "mutation" in (r.stdout + r.stderr)
    assert r.returncode != 0
