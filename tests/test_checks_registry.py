"""Tests for the CHECKS registry and the --require resolution.

The registry is the single source of truth for check names and the
evidence key each check writes. These tests guard its internal
consistency and the deduplication rules of contract --require.
"""

import sys
from pathlib import Path

import pytest

from conftest import read_state

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


# --- registry consistency -------------------------------------------------

def test_every_check_declares_the_full_schema():
    # "extract_from" joined the schema with the property check: it names the
    # text the extractor reads, the captured output for a check that prints
    # its number, the command string for one whose number is an input to the
    # run (see extract_hypothesis_seed).
    for name, spec in red_proof.CHECKS.items():
        assert set(spec) == {"evidence_key", "staleness", "extract",
                             "extract_from"}, name
        assert spec["evidence_key"], name
        assert spec["extract_from"] in ("output", "command"), name


def test_evidence_keys_are_unique():
    keys = [spec["evidence_key"] for spec in red_proof.CHECKS.values()]
    assert len(keys) == len(set(keys))


def test_the_base_evidence_keys_are_reachable_or_attested():
    reachable = {spec["evidence_key"] for spec in red_proof.CHECKS.values()}
    for key in red_proof.BASE_EVIDENCE:
        assert key in reachable or key == "attest", key


# --- required_evidence ----------------------------------------------------

def test_no_require_yields_the_base_set():
    assert red_proof.required_evidence(None) == list(red_proof.BASE_EVIDENCE)


def test_require_keeps_the_order_of_mention():
    assert red_proof.required_evidence("static,full-suite") == [
        "targeted", "full_suite", "attest", "static"]


@pytest.mark.parametrize("spec", ["targeted", "static,static", "targeted,targeted"])
def test_require_never_duplicates_a_key(spec):
    keys = red_proof.required_evidence(spec)
    assert len(keys) == len(set(keys))


# --- the same rules through the CLI ---------------------------------------

@pytest.fixture
def repo(git_repo):
    return git_repo()


def test_require_targeted_adds_nothing(rp, claude_home, repo):
    r = rp(["contract", "--file", "contract.md", "--require", "targeted"],
           cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert read_state(claude_home, repo)["required_evidence"] == [
        "targeted", "full_suite", "attest"]


def test_a_repeated_require_is_recorded_once(rp, claude_home, repo):
    r = rp(["contract", "--file", "contract.md", "--require", "static,static"],
           cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert read_state(claude_home, repo)["required_evidence"] == [
        "targeted", "full_suite", "attest", "static"]


def test_a_rejected_require_leaves_the_previous_state_intact(
        rp, claude_home, repo):
    assert rp(["contract", "--file", "contract.md", "--require", "static"],
              cwd=repo).returncode == 0
    before = read_state(claude_home, repo)

    r = rp(["contract", "--file", "contract.md", "--require", "static,nosuch"],
           cwd=repo)

    assert r.returncode == 1
    assert "nosuch" in r.stdout
    assert read_state(claude_home, repo) == before
