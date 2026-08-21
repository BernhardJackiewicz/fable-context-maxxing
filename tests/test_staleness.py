"""Tests for the non-production classification and its two readers.

is_nonprod is the one rule that decides what counts as production code;
production_fingerprint is the fingerprint that applies it, and
staleness_policy/is_stale are what the commit gate reads it through.
"""

import sys
from pathlib import Path

import pytest

from conftest import git

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


# --- is_nonprod -----------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "tests/helper.py",
    "pkg/tests/helper.py",
    "test/helper.py",
    "spec/helper.py",
    "docs/gen.py",
    "doc/gen.py",
    "examples/demo.py",
    ".claude/helper.py",
    ".red-proof/helper.py",
    "scratchpad/helper.py",
    "test_thing.py",
    "pkg/test_thing.py",
    "conftest.py",
    "pkg/conftest.py",
    "README.md",
    "guide.rst",
    "notes.txt",
    "thing_test.py",
    "thing_test.go",
    "thing.spec.ts",
    "thing.spec.js",
    "thing.spec.tsx",
    "thing.test.ts",
    "thing.test.js",
    "thing.test.tsx",
])
def test_a_non_production_path_is_recognized(rel):
    assert red_proof.is_nonprod(rel)


@pytest.mark.parametrize("rel", [
    "app.py",
    "bench/bench.py",
    "bin/red_proof.py",
    "contests/entry.py",     # a marker matches whole segments, not substrings
    "documentation/gen.py",  # "/doc/" is a segment, "documentation" is not
    "protest.py",            # the filename rule needs "test_", not "test"
    "spectrum.py",
])
def test_a_production_path_is_recognized(rel):
    assert not red_proof.is_nonprod(rel)


@pytest.mark.parametrize("rel", [
    "tests\\helper.py",
    "pkg\\tests\\helper.py",
    "pkg\\test_thing.py",
    "docs\\gen.py",
])
def test_windows_separators_are_tolerated(rel):
    assert red_proof.is_nonprod(rel)


def test_a_windows_path_to_production_code_stays_production():
    assert not red_proof.is_nonprod("pkg\\app.py")


@pytest.mark.parametrize("rel", [
    "./tests/helper.py",
    "././README.md",
    "./test_thing.py",
    "/tests/helper.py",
])
def test_a_leading_dot_or_slash_is_tolerated(rel):
    assert red_proof.is_nonprod(rel)


@pytest.mark.parametrize("rel", ["./app.py", "/app.py", "././bench/bench.py"])
def test_a_leading_dot_or_slash_keeps_production_production(rel):
    assert not red_proof.is_nonprod(rel)


def test_a_feature_file_is_non_production_wherever_it_sits():
    # ".feature" is a non-production suffix: a Gherkin file is
    # specification, written before the code, so its directory does not
    # decide. This replaces the earlier characterization, in which a
    # feature file outside an excluded directory counted as production.
    assert red_proof.is_nonprod("login.feature")
    assert red_proof.is_nonprod("features/login.feature")
    assert red_proof.is_nonprod("features/auth/login.feature")
    assert red_proof.is_nonprod("tests/login.feature")
    assert red_proof.is_nonprod("spec/login.feature")


# --- production_fingerprint -----------------------------------------------

@pytest.fixture
def repo(git_repo):
    return git_repo(files={
        "test_core.py": "def test_add():\n    assert True\n",
        "docs/guide.md": "guide\n",
        "NOTES.md": "notes\n",
    })


def prod_fp(repo):
    return red_proof.production_fingerprint(str(repo))


def test_a_clean_tree_has_a_stable_production_fingerprint(repo):
    assert prod_fp(repo) == prod_fp(repo)
    assert len(prod_fp(repo)) == 64


def test_renaming_a_test_file_leaves_the_production_fingerprint_intact(repo):
    before, full_before = prod_fp(repo), red_proof.fingerprint(str(repo))

    git(repo, "mv", "test_core.py", "test_renamed.py")

    # Both sides of the rename are reported, and both are non-production.
    assert red_proof.fingerprint(str(repo)) != full_before
    assert prod_fp(repo) == before


def test_renaming_a_production_file_changes_the_production_fingerprint(repo):
    before = prod_fp(repo)
    git(repo, "mv", "core.py", "renamed.py")
    assert prod_fp(repo) != before


def test_untracked_non_production_files_are_ignored(repo):
    before, full_before = prod_fp(repo), red_proof.fingerprint(str(repo))

    (repo / "test_new.py").write_text("def test_new():\n    assert True\n")
    (repo / "docs" / "extra.md").write_text("extra\n")

    assert red_proof.fingerprint(str(repo)) != full_before
    assert prod_fp(repo) == before


def test_an_untracked_production_file_changes_the_production_fingerprint(repo):
    before = prod_fp(repo)
    (repo / "fresh.py").write_text("x = 1\n")
    assert prod_fp(repo) != before


def test_documentation_and_feature_edits_are_both_ignored(repo):
    before, full_before = prod_fp(repo), red_proof.fingerprint(str(repo))

    (repo / "NOTES.md").write_text("changed notes\n")
    (repo / "docs" / "guide.md").write_text("changed guide\n")
    assert prod_fp(repo) == before

    # A Gherkin file is specification too, in any directory. Earlier this
    # file was production and changed the production fingerprint here.
    (repo / "login.feature").write_text("Feature: login\n")
    (repo / "features").mkdir()
    (repo / "features" / "signup.feature").write_text("Feature: signup\n")
    assert prod_fp(repo) == before

    # The full fingerprint still sees every one of those files.
    assert red_proof.fingerprint(str(repo)) != full_before


def test_a_deleted_production_file_changes_the_production_fingerprint(repo):
    before = prod_fp(repo)
    (repo / "core.py").unlink()
    assert prod_fp(repo) != before


def test_a_deleted_test_file_does_not(repo):
    before = prod_fp(repo)
    (repo / "test_core.py").unlink()
    assert prod_fp(repo) == before


def test_staging_does_not_change_the_production_fingerprint(repo):
    (repo / "core.py").write_text("def add(a, b):\n    return a - b\n")
    before = prod_fp(repo)
    git(repo, "add", "-A")
    assert prod_fp(repo) == before


# --- staleness policy -----------------------------------------------------

@pytest.mark.parametrize("key, policy", [
    ("targeted", "strict"),
    ("full_suite", "strict"),
    ("static", "strict"),
    ("coverage", "strict"),
    ("mutation", "production"),
    ("attest", "strict"),
    ("commit_ready", "strict"),
    ("nothing_declares_this", "strict"),
])
def test_the_policy_of_an_evidence_key(key, policy):
    assert red_proof.staleness_policy(key) == policy


def test_a_strict_key_ignores_the_production_fingerprint():
    item = {"fingerprint": "a", "production_fingerprint": "p"}
    assert red_proof.is_stale(item, "targeted", "b", "p")
    assert not red_proof.is_stale(item, "targeted", "a", "q")


def test_a_production_key_reads_the_production_fingerprint():
    item = {"fingerprint": "a", "production_fingerprint": "p"}
    assert not red_proof.is_stale(item, "mutation", "b", "p")
    assert red_proof.is_stale(item, "mutation", "a", "q")


@pytest.mark.parametrize("item", [
    {"fingerprint": "a"},
    {"fingerprint": "a", "production_fingerprint": ""},
    {"fingerprint": "a", "production_fingerprint": None},
])
def test_evidence_without_a_production_fingerprint_is_judged_strictly(item):
    assert red_proof.is_stale(item, "mutation", "b", "p")
    assert not red_proof.is_stale(item, "mutation", "a", "q")
