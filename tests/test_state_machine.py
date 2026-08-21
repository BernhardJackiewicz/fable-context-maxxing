"""Subprocess characterization tests for the gate state machine.

CONTRACT_CREATED -> RED_CONFIRMED -> TESTS_FROZEN -> COMMIT_ISSUED, plus
the evidence binding to the code fingerprint. State is read back from
<config>/red-proof/state/<key>.json.
"""

import hashlib
import json
import sys

import pytest

from conftest import ACCEPTANCE_TEST_BODY, freeze_cycle, git, read_state

IMPORT_NEWMOD = [sys.executable, "-c", "import newmod"]
ASSERT_NEWMOD = [sys.executable, "-c", "import newmod; assert newmod.x == 1"]


@pytest.fixture
def repo(git_repo):
    return git_repo()


@pytest.fixture
def frozen_repo(rp, claude_home, git_repo):
    repo = freeze_cycle(rp, claude_home, git_repo())
    (repo / "newmod.py").write_text("x = 1\n")
    return repo


def phase(claude_home, repo):
    return read_state(claude_home, repo).get("phase")


def evidence(claude_home, repo):
    return read_state(claude_home, repo).get("evidence", {})


def record_all_evidence(rp, repo):
    for name, cmd in (("targeted", ASSERT_NEWMOD), ("full-suite", IMPORT_NEWMOD)):
        r = rp(["check", name, "--"] + cmd, cwd=repo)
        assert r.returncode == 0, r.stdout + r.stderr
    r = rp(["attest", "--diff-reviewed", "--contract-ok"], cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr


# --- contract -------------------------------------------------------------

def test_contract_registers_hash_and_phase(rp, claude_home, repo):
    r = rp(["contract", "--file", "contract.md"], cwd=repo)

    assert r.returncode == 0, r.stdout + r.stderr
    state = read_state(claude_home, repo)
    assert state["phase"] == "CONTRACT_CREATED"
    assert state["contract_hash"] == hashlib.sha256(
        (repo / "contract.md").read_bytes()).hexdigest()
    assert state["red_proofs"] == [] and state["evidence"] == {}


def test_contract_requires_an_existing_file(rp, repo):
    r = rp(["contract", "--file", "missing.md"], cwd=repo)
    assert r.returncode == 1
    assert "usage: contract" in r.stdout


# --- red ------------------------------------------------------------------

def test_red_requires_a_registered_contract(rp, repo):
    r = rp(["red", "--test", "t", "--type", "contract", "--expected", "x",
            "--"] + IMPORT_NEWMOD, cwd=repo)
    assert r.returncode == 1
    assert "red requires phase CONTRACT_CREATED" in r.stdout


def test_red_refuses_a_command_that_exits_zero(rp, claude_home, repo):
    assert rp(["contract", "--file", "contract.md"], cwd=repo).returncode == 0

    r = rp(["red", "--test", "bogus", "--type", "behavior",
            "--expected", "must not be recorded",
            "--", sys.executable, "-c", "pass"], cwd=repo)

    assert r.returncode == 1
    assert "not a valid red" in r.stdout
    assert phase(claude_home, repo) == "CONTRACT_CREATED"
    assert read_state(claude_home, repo)["red_proofs"] == []


def test_red_records_a_real_failure(rp, claude_home, repo):
    assert rp(["contract", "--file", "contract.md"], cwd=repo).returncode == 0

    r = rp(["red", "--test", "test_newmod", "--type", "contract",
            "--expected", "ModuleNotFoundError: newmod",
            "--"] + IMPORT_NEWMOD, cwd=repo)

    assert r.returncode == 0, r.stdout + r.stderr
    state = read_state(claude_home, repo)
    assert state["phase"] == "RED_CONFIRMED"
    proof = state["red_proofs"][0]
    assert proof["test"] == "test_newmod" and proof["exit_code"] != 0
    assert "ModuleNotFoundError" in proof["actual_output_tail"]


# --- freeze ---------------------------------------------------------------

def test_freeze_requires_staged_tests(rp, claude_home, repo):
    assert rp(["contract", "--file", "contract.md"], cwd=repo).returncode == 0
    assert rp(["red", "--test", "test_newmod", "--type", "contract",
               "--expected", "ModuleNotFoundError: newmod",
               "--"] + IMPORT_NEWMOD, cwd=repo).returncode == 0
    (repo / "test_newmod.py").write_text(ACCEPTANCE_TEST_BODY)

    r = rp(["freeze"], cwd=repo)

    assert r.returncode == 1
    assert "nothing staged" in r.stdout
    assert phase(claude_home, repo) == "RED_CONFIRMED"


def test_freeze_records_the_staged_patch(rp, claude_home, git_repo):
    repo = freeze_cycle(rp, claude_home, git_repo())

    state = read_state(claude_home, repo)
    assert state["phase"] == "TESTS_FROZEN"
    assert state["freeze"]["paths"] == ["test_newmod.py"]
    assert len(state["freeze"]["patch_hash"]) == 64
    assert rp(["check", "freeze"], cwd=repo).returncode == 0


def test_freeze_check_detects_a_weakened_test(rp, claude_home, git_repo):
    repo = freeze_cycle(rp, claude_home, git_repo())
    (repo / "test_newmod.py").write_text("def test_x():\n    assert True\n")

    r = rp(["check", "freeze"], cwd=repo)

    assert r.returncode == 1
    assert "working-tree modification" in r.stdout


# --- check ----------------------------------------------------------------

def test_check_requires_the_frozen_phase(rp, repo):
    r = rp(["check", "targeted", "--"] + IMPORT_NEWMOD, cwd=repo)
    assert r.returncode == 1
    assert "requires phase TESTS_FROZEN" in r.stdout


def test_checks_bind_evidence_to_the_current_fingerprint(rp, claude_home, frozen_repo):
    record_all_evidence(rp, frozen_repo)

    ev = evidence(claude_home, frozen_repo)
    fingerprints = {ev[k]["fingerprint"] for k in ("targeted", "full_suite", "attest")}
    assert len(fingerprints) == 1 and len(fingerprints.pop()) == 64
    status = json.loads(rp(["status"], cwd=frozen_repo).stdout)
    assert status["evidence"]["targeted"]["fingerprint"] == status["current_fingerprint"]
    assert status["phase"] == "TESTS_FROZEN"


def test_a_failing_check_records_no_evidence(rp, claude_home, frozen_repo):
    r = rp(["check", "targeted", "--", sys.executable, "-c",
            "import newmod; assert newmod.x == 99"], cwd=frozen_repo)

    assert r.returncode == 1
    assert "evidence NOT recorded" in r.stdout
    assert "targeted" not in evidence(claude_home, frozen_repo)


# --- attest ---------------------------------------------------------------

@pytest.mark.parametrize("args", [["attest"], ["attest", "--diff-reviewed"]])
def test_attest_requires_both_flags(rp, claude_home, frozen_repo, args):
    r = rp(args, cwd=frozen_repo)
    assert r.returncode == 1
    assert "usage: attest" in r.stdout
    assert "attest" not in evidence(claude_home, frozen_repo)


# --- commit gate ----------------------------------------------------------

def test_commit_gate_lists_every_missing_piece(rp, frozen_repo):
    r = rp(["commit-gate"], cwd=frozen_repo)

    assert r.returncode == 1
    for missing in ("targeted", "full_suite", "attest"):
        assert "missing evidence: " + missing in r.stdout


def test_commit_gate_passes_and_a_code_change_invalidates_it(
        rp, claude_home, frozen_repo):
    record_all_evidence(rp, frozen_repo)

    r = rp(["commit-gate"], cwd=frozen_repo)
    assert r.returncode == 0, r.stdout + r.stderr
    ready = evidence(claude_home, frozen_repo)["commit_ready"]
    assert ready["fingerprint"] == evidence(
        claude_home, frozen_repo)["targeted"]["fingerprint"]

    (frozen_repo / "newmod.py").write_text("x = 1\ny = 2\n")

    stale = rp(["commit-gate"], cwd=frozen_repo)
    assert stale.returncode == 1
    assert "stale evidence" in stale.stdout
    hook = rp(["hook", "bash"], cwd=frozen_repo, stdin=json.dumps(
        {"tool_input": {"command": "git commit -m x"}, "cwd": str(frozen_repo)}))
    assert "stale" in hook.stdout


def test_a_passed_gate_closes_the_cycle_on_the_commit(rp, claude_home, frozen_repo):
    record_all_evidence(rp, frozen_repo)
    assert rp(["commit-gate"], cwd=frozen_repo).returncode == 0

    payload = json.dumps({"tool_input": {"command": "git add -A && git commit -m feat"},
                          "cwd": str(frozen_repo)})
    allowed = rp(["hook", "bash"], cwd=frozen_repo, stdin=payload)
    assert allowed.stdout.strip() == ""

    git(frozen_repo, "add", "-A")
    git(frozen_repo, "commit", "-qm", "feat: add newmod")
    state = read_state(claude_home, frozen_repo)
    assert state["phase"] == "COMMIT_ISSUED"
    assert "commit_ready" not in state["evidence"]

    denied = rp(["hook", "bash"], cwd=frozen_repo, stdin=payload)
    assert '"deny"' in denied.stdout
