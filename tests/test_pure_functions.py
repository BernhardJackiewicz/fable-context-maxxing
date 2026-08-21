"""In-process characterization tests for the base-directory independent
helpers of the gate CLI.

BASE_DIR and the constants derived from it are evaluated at import time,
so anything that reads or writes gate state must go through a subprocess
instead (see test_hooks.py and test_state_machine.py). Nothing exercised
here touches the base directory.
"""

import os
import sys
from pathlib import Path

import pytest

from conftest import git

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


def real(path):
    return os.path.realpath(str(path))


# --- parse_opts -----------------------------------------------------------

def test_parse_opts_reads_values_and_bare_flags():
    opts, rest = red_proof.parse_opts(["--file", "contract.md", "--force"])
    assert opts == {"file": "contract.md", "force": True}
    assert rest == []


def test_parse_opts_normalizes_dashes_to_underscores():
    opts, _ = red_proof.parse_opts(["--diff-reviewed", "--contract-ok"])
    assert opts == {"diff_reviewed": True, "contract_ok": True}


def test_parse_opts_treats_double_dash_as_start_of_command():
    opts, rest = red_proof.parse_opts(
        ["--test", "t", "--", sys.executable, "-c", "pass"])
    assert opts == {"test": "t"}
    assert rest == [sys.executable, "-c", "pass"]


def test_parse_opts_ignores_positionals_and_an_empty_command():
    opts, rest = red_proof.parse_opts(["stray", "--hours", "2", "--"])
    assert opts == {"hours": "2"}
    assert rest == []


# --- strip_quoted and GIT_COMMIT_RE ---------------------------------------

def matches_commit(command):
    return bool(red_proof.GIT_COMMIT_RE.search(red_proof.strip_quoted(command)))


@pytest.mark.parametrize("command", [
    "git commit -m x",
    "git add -A && git commit -m feat",
    "git -C /some/repo commit -m x",
    "git -c user.email=a@b commit -m x",
    "cd /some/repo && git commit --amend --no-edit",
])
def test_commit_commands_are_detected(command):
    assert matches_commit(command)


@pytest.mark.parametrize("command", [
    "echo 'git commit'",
    'echo "git commit"',
    "git status",
    "git commitment -m x",
    "echo gitcommit",
    "ls -la",
])
def test_non_commit_commands_are_not_detected(command):
    assert not matches_commit(command)


def test_strip_quoted_blanks_quoted_spans_only():
    assert red_proof.strip_quoted("echo 'git commit' && git commit") == \
        "echo '' && git commit"
    assert red_proof.strip_quoted('a "b c" d') == 'a "" d'


# --- bash_target_root -----------------------------------------------------

def test_bash_target_root_follows_a_leading_cd(git_repo):
    a, b = git_repo("a"), git_repo("b")
    command = "cd %s && git commit -m x" % b
    assert red_proof.bash_target_root(command, str(a)) == real(b)


def test_bash_target_root_honors_git_c_on_the_commit(git_repo):
    a, b = git_repo("a"), git_repo("b")
    command = "git -C %s commit -m x" % b
    assert red_proof.bash_target_root(command, str(a)) == real(b)


def test_bash_target_root_ignores_git_c_on_another_subcommand(git_repo):
    a, b = git_repo("a"), git_repo("b")
    command = "git -C %s status && git commit -m x" % b
    assert red_proof.bash_target_root(command, str(a)) == real(a)


def test_bash_target_root_falls_back_when_the_hint_does_not_exist(git_repo):
    a = git_repo("a")
    command = "cd %s/nowhere && git commit -m x" % a
    assert red_proof.bash_target_root(command, str(a)) == real(a)


def test_bash_target_root_uses_the_cwd_without_any_hint(git_repo):
    a = git_repo("a")
    assert red_proof.bash_target_root("git commit -m x", str(a)) == real(a)


def test_bash_target_root_is_none_outside_a_repository(tmp_path):
    assert red_proof.bash_target_root("git commit -m x", str(tmp_path)) is None


# --- changed_paths --------------------------------------------------------

def test_changed_paths_is_empty_for_a_clean_tree(git_repo):
    assert red_proof.changed_paths(str(git_repo())) == []


def test_changed_paths_reports_modified_and_untracked_files(git_repo):
    repo = git_repo()
    (repo / "core.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "fresh.py").write_text("x = 1\n")
    assert set(red_proof.changed_paths(str(repo))) == {"core.py", "fresh.py"}


def test_changed_paths_reports_both_sides_of_a_rename(git_repo):
    repo = git_repo()
    git(repo, "mv", "core.py", "renamed.py")
    assert set(red_proof.changed_paths(str(repo))) == {"core.py", "renamed.py"}


# --- fingerprint ----------------------------------------------------------

def test_fingerprint_is_stable_for_an_unchanged_tree(git_repo):
    repo = git_repo()
    assert red_proof.fingerprint(str(repo)) == red_proof.fingerprint(str(repo))


def test_fingerprint_is_unchanged_by_staging(git_repo):
    repo = git_repo()
    (repo / "core.py").write_text("def add(a, b):\n    return a - b\n")
    before = red_proof.fingerprint(str(repo))
    git(repo, "add", "-A")
    assert red_proof.fingerprint(str(repo)) == before


def test_fingerprint_changes_with_file_content(git_repo):
    repo = git_repo()
    (repo / "core.py").write_text("def add(a, b):\n    return a - b\n")
    before = red_proof.fingerprint(str(repo))
    (repo / "core.py").write_text("def add(a, b):\n    return a * b\n")
    assert red_proof.fingerprint(str(repo)) != before


def test_fingerprint_counts_untracked_files(git_repo):
    repo = git_repo()
    before = red_proof.fingerprint(str(repo))
    (repo / "fresh.py").write_text("x = 1\n")
    assert red_proof.fingerprint(str(repo)) != before


# --- verify_freeze --------------------------------------------------------

def frozen_state(repo, paths):
    return {"freeze": {"paths": paths,
                       "patch_hash": red_proof.staged_patch_hash(str(repo), paths)}}


def staged_acceptance_repo(git_repo):
    repo = git_repo()
    (repo / "test_newmod.py").write_text("def test_x():\n    assert False\n")
    git(repo, "add", "test_newmod.py")
    return repo, frozen_state(repo, ["test_newmod.py"])


def test_verify_freeze_rejects_a_missing_freeze(git_repo):
    good, msg = red_proof.verify_freeze({}, str(git_repo()))
    assert good is False
    assert "no freeze recorded" in msg


def test_verify_freeze_accepts_an_intact_staged_patch(git_repo):
    repo, state = staged_acceptance_repo(git_repo)
    good, msg = red_proof.verify_freeze(state, str(repo))
    assert good is True, msg


def test_verify_freeze_detects_a_restaged_patch(git_repo):
    repo, state = staged_acceptance_repo(git_repo)
    (repo / "test_newmod.py").write_text("def test_x():\n    assert True\n")
    git(repo, "add", "test_newmod.py")
    good, msg = red_proof.verify_freeze(state, str(repo))
    assert good is False
    assert "byte-identical" in msg


def test_verify_freeze_detects_a_working_tree_change(git_repo):
    repo, state = staged_acceptance_repo(git_repo)
    (repo / "test_newmod.py").write_text("def test_x():\n    assert True\n")
    good, msg = red_proof.verify_freeze(state, str(repo))
    assert good is False
    assert "working-tree modification" in msg
